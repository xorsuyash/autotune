import ast
import io
import logging
from decimal import Decimal, getcontext
from typing import List

import pandas as pd
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from huggingface_hub import HfApi
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView, UpdateAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from workflow.generator.dataFetcher import DataFetcher
from workflow.generator.generate import process_task
from workflow.training.train import train

from .mixins import CacheDatasetMixin, UserIDMixin
from .models import (
    Dataset,
    DatasetData,
    Examples,
    MLModel,
    Prompt,
    Task,
    User,
    WorkflowConfig,
    Workflows,
)
from .serializers import (
    DatasetDataSerializer,
    ExampleSerializer,
    MLModelSerializer,
    ModelDataSerializer,
    PromptSerializer,
    UserSerializer,
    WorkflowConfigSerializer,
    WorkflowDetailSerializer,
    WorkflowSerializer,
)
from .utils import (
    create_pydantic_model,
    dehydrate_cache,
    get_model_cost,
    get_task_config,
    get_task_mapping,
    paginate_queryset,
    validate_and_save_examples,
)

logger = logging.getLogger(__name__)


def index():
    return HttpResponse("Hello, world. You're at the workflow index.")


class CreateWorkflowView(UserIDMixin, APIView):

    def post(self, request):
        with transaction.atomic():
            user: User = request.META["user"]

            workflow_data = request.data.get("workflow")
            workflow_data["user"] = user.user_id
            workflow_serializer = WorkflowSerializer(
                data=request.data.get("workflow", {})
            )
            if workflow_serializer.is_valid(raise_exception=True):
                workflow = workflow_serializer.save()

                prompt_data = {
                    "user_prompt": request.data.get("user_prompt", ""),
                    "workflow": workflow.pk,
                }

                prompt_serializer = PromptSerializer(data=prompt_data)
                if prompt_serializer.is_valid(raise_exception=True):
                    prompt_serializer.save()

                    return Response(
                        {
                            "workflow": workflow_serializer.data,
                            "prompt": prompt_serializer.data,
                        },
                        status=status.HTTP_201_CREATED,
                    )

        return Response(
            {
                "error": "Invalid data for workflow or prompt",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )


@method_decorator(csrf_exempt, name="dispatch")
class IterateWorkflowView(UserIDMixin, APIView):
    """
    Iterates over a workflow by either adding new examples or refining existing ones based on the provided data.
    This operation can generate or refine questions and answers based on the examples associated with the workflow.

    Args:
        request (HttpRequest): The request object containing 'examples' data.
        workflow_id (int): The ID of the workflow to be iterated on.

    Sample Request Payload:
        {
            "examples": [
                {
                    "text": "What is AI?",
                    "label": "positive",
                    "reason": "Relevant to the field of study"
                },
                {
                    "text": "What is 2 + 2?",
                    "label": "negative",
                    "reason": "Irrelevant question"
                }
            ]
        }
    Returns:
    - A response object with the outcome of the iteration process. The response structure and data depend on the json schema defined in the configfunction.
    """

    def post(self, request, workflow_id, *args, **kwargs):
        user_id = request.META["user"].user_id

        workflow = get_object_or_404(
            Workflows, workflow_id=workflow_id, user_id=user_id
        )
        workflow.status = "ITERATION"
        workflow.save()
        examples_data = request.data.get("examples", [])

        examples_exist = (
            Examples.objects.filter(
                workflow_id=workflow_id, label__isnull=False
            ).exists()
            or len(examples_data) > 0
        )

        Model, _ = create_pydantic_model(workflow.workflow_config.schema_example)

        success, result = validate_and_save_examples(examples_data, Model, workflow)

        if not success:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        user_prompt = request.data.get("user_prompt")
        if user_prompt:
            Prompt.objects.create(user_prompt=user_prompt, workflow=workflow)

        total_examples = request.data.get("total_examples", 10)
        max_iterations = request.data.get("max_iterations", 50)
        max_concurrent_fetches = request.data.get("max_concurrent_fetches", 100)
        batch_size = request.data.get("batch_size", 5)

        fetcher = DataFetcher(
            max_iterations=int(max_iterations),
            max_concurrent_fetches=int(max_concurrent_fetches),
            batch_size=int(batch_size),
        )
        prompt: Prompt = workflow.latest_prompt
        fetcher.generate_or_refine(
            workflow_id=workflow.workflow_id,
            total_examples=total_examples,
            workflow_config_id=workflow.workflow_config.id,
            llm_model=workflow.llm_model,
            Model=Model,
            prompt=prompt.user_prompt,
            prompt_id=prompt.id,
            refine=examples_exist,
            iteration=1,
        )

        costs = get_model_cost(workflow.llm_model)

        getcontext().prec = 6

        input_cost = Decimal(fetcher.input_tokens * costs["input"]) / Decimal(1000)
        output_cost = Decimal(fetcher.output_tokens * costs["output"]) / Decimal(1000)

        iteration_cost = input_cost + output_cost
        iteration_cost = iteration_cost.quantize(Decimal("0.0001"))
        workflow.cost += iteration_cost
        workflow.cost = workflow.cost.quantize(Decimal("0.0001"))

        total_batches = max(
            1,
            (workflow.total_examples + batch_size - 1) // batch_size,
        )

        workflow.estimated_dataset_cost = Decimal(
            Decimal(1.25) * iteration_cost * total_batches
        )

        workflow.estimated_dataset_cost = workflow.estimated_dataset_cost.quantize(
            Decimal("0.0001")
        )

        workflow.status = "IDLE"
        workflow.save()
        return Response(
            {
                "workflow_cost": f"${workflow.cost}",
                "iteration_cost": f"${iteration_cost}",
                "estimated_dataset_cost": f"${workflow.estimated_dataset_cost}",
                "data": fetcher.examples,
            }
        )


class WorkflowListView(APIView):
    def get(self, request, *args, **kwargs):
        workflows = Workflows.objects.all()
        serializer = WorkflowDetailSerializer(workflows, many=True)
        return Response(serializer.data)

    def post(self, request, *args, **kwargs):
        serializer = WorkflowSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SingleWorkflowView(APIView):
    def get(self, request, workflow_id, *args, **kwargs):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        serializer = WorkflowDetailSerializer(workflow)
        return Response(serializer.data)

    def put(self, request, workflow_id, *args, **kwargs):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        serializer = WorkflowSerializer(workflow, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, workflow_id, *args, **kwargs):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        workflow.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PromptViewSet(APIView):
    def get(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, pk=workflow_id)
        prompts = (
            workflow.prompts.all()
        )  # Get all prompts associated with this workflow
        return Response(PromptSerializer(prompts, many=True).data)

    def post(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, pk=workflow_id)
        if not request.data.get("user_prompt"):
            return Response(
                {"message": "user_prompt is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        prompt_data = {
            "user_prompt": request.data.get("user_prompt"),
            "workflow": workflow.pk,
        }
        serializer = PromptSerializer(data=prompt_data)
        if serializer.is_valid():
            prompt = serializer.save(workflow=workflow)

            # Update the latest_prompt field on the workflow to this new prompt
            workflow.latest_prompt = prompt
            workflow.save()

            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)


class ExamplesView(APIView):

    def get(self, request, workflow_id=None):
        if workflow_id:
            examples = Examples.objects.filter(
                workflow_id=workflow_id, task_id__isnull=True
            )
        else:
            examples = Examples.objects.all()

        serialized_examples = ExampleSerializer(examples, many=True)
        return Response(serialized_examples.data, status=status.HTTP_200_OK)

    def post(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, pk=workflow_id)
        examples_data = request.data.get("examples", [])

        Model, _ = create_pydantic_model(workflow.workflow_config.schema_example)

        success, result = validate_and_save_examples(examples_data, Model, workflow)

        if not success:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": "Examples updated successfully"}, status=201)


class WorkflowUpdateView(UpdateAPIView):
    """
    Update an existing workflow.

    PUT /workflow/{workflow_id}/update/

    Parameters:
    - workflow_id (URL Path): ID of the workflow to be updated.

    Request Body (application/json):
    {
        "workflow_name": "New Workflow Name",
        "total_examples": 1200,
        ...
    }

    Responses:
    - 200 OK: Workflow successfully updated.
      {
          "workflow_name": "New Workflow Name",
          "total_examples": 1200,
          ...
      }
    - 404 Not Found: If no workflow with the given ID exists.
    """

    queryset = Workflows.objects.all()
    serializer_class = WorkflowSerializer
    lookup_field = "workflow_id"


class WorkflowDuplicateView(APIView):
    """
    Duplicate an existing workflow, creating a new instance with a new ID.

    PUT /workflow/{workflow_id}/duplicate/

    Parameters:
    - workflow_id (URL Path): ID of the workflow to be duplicated.

    Responses:
    - 201 Created: Workflow successfully duplicated.
      {
          "workflow_id": "new-workflow-id",
          ...
      }
    - 404 Not Found: If no workflow with the given ID exists.
    """

    def put(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        workflow.pk = None
        workflow.save()
        serializer = WorkflowSerializer(workflow)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkflowStatusView(APIView):
    """
    Retrieve the status of a specific workflow.

    GET /workflow/status/{workflow_id}/

    Parameters:
    - workflow_id (URL Path): ID of the workflow whose status is to be retrieved.

    Responses:
    - 200 OK: Successfully retrieved the status of the workflow.
      {
          "workflow_id": "workflow-id",
          "status": "Workflow Status"
      }
    - 404 Not Found: If no workflow with the given ID exists.
    """

    def get(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        return Response({"status": workflow.status})


class WorkflowSearchView(ListAPIView):
    """
    Search for workflows by tag or name.

    GET /workflow/q/?tags=tag1,tag2

    Query Parameters:
    - tags (string): Comma-separated list of tags to filter workflows by.

    Responses:
    - 200 OK: Returns a list of workflows that match the search criteria.
      [
          {
              "workflow_id": "some-workflow-id",
              "workflow_name": "Some Workflow Name",
              ...
          },
          ...
      ]
    """

    serializer_class = WorkflowSerializer

    def get_queryset(self):
        tags_param = self.request.query_params.get("tags", "")
        tags_query = tags_param.split(",") if tags_param else []
        query = Q(tags__overlap=tags_query) if tags_query else Q()
        return Workflows.objects.filter(query)


class TaskView(APIView):

    def get(self, request, task_id):
        task = get_object_or_404(Task, pk=task_id)
        percentage = task.generated_samples / task.total_samples
        return Response({"status": task.status, "percentage": percentage})


@method_decorator(csrf_exempt, name="dispatch")
class GenerateTaskView(UserIDMixin, APIView):
    def post(self, request, workflow_id, *args, **kwargs):
        user_id = request.META["user"].user_id

        workflow = get_object_or_404(
            Workflows, workflow_id=workflow_id, user_id=user_id
        )

        uploaded_files = request.FILES.getlist("file")
        prompts_str = request.data.get("prompts")

        prompts = []
        total_examples = request.data.get("total_examples")
        batch_size = request.data.get("batch_size")

        if uploaded_files and prompts_str:
            return JsonResponse(
                {
                    "error": "Both prompts and file uploads are not allowed. Please provide either prompts or a file."
                },
                status=400,
            )
        if len(uploaded_files) > 0:
            if not request.data.get("example_per_prompt"):
                return JsonResponse(
                    {"error": "example_per_prompt is required when uploading files."},
                    status=400,
                )
            total_examples = request.data.get("example_per_prompt")
            batch_size = total_examples

            series = []
            for file in uploaded_files:
                if not file.name.lower().endswith(".csv"):
                    return JsonResponse(
                        {
                            "error": f"Invalid file extension for {file.name}. Only .csv files are allowed."
                        },
                        status=400,
                    )

                try:
                    csv_file = io.BytesIO(file.read())
                    df = pd.read_csv(csv_file)
                    if "prompts" in df:
                        series.append(df["prompts"])
                    else:
                        return JsonResponse(
                            {"error": " `prompts` column not found in the CSV file"},
                            status=400,
                        )
                except Exception as e:
                    return JsonResponse(
                        {"error": f"Error reading CSV file: {str(e)}"}, status=400
                    )
            prompts = pd.concat(series, ignore_index=True).tolist()

        if prompts_str:
            if not request.data.get("example_per_prompt"):
                return JsonResponse(
                    {"error": "example_per_prompt is required when providing prompts."},
                    status=400,
                )
            total_examples = request.data.get("example_per_prompt")
            batch_size = total_examples

            try:
                prompts = ast.literal_eval(prompts_str)
                if not isinstance(prompts, list):
                    raise ValueError("Prompts is not a valid list.")
            except (ValueError, SyntaxError) as e:
                return JsonResponse(
                    {"error": f"Invalid prompts format: {str(e)}"}, status=400
                )

        if total_examples:
            workflow.total_examples = total_examples
            workflow.save()

        max_iterations = request.data.get("max_iterations", 50)
        max_concurrent_fetches = request.data.get("max_concurrent_fetches", 100)
        batch_size = batch_size if batch_size else 5

        task = Task.objects.create(
            name=f"Batch Task for Workflow {workflow_id}",
            status="STARTING",
            workflow=workflow,
        )

        process_task.delay(
            task.id,
            int(max_iterations),
            int(max_concurrent_fetches),
            int(batch_size),
            prompts,
        )

        estimated_cost = workflow.estimated_dataset_cost

        if estimated_cost == None:
            estimated_cost = "Not available without iterations being completed"

        return JsonResponse(
            {
                "message": "Tasks creation initiated",
                "task_id": task.id,
                "workflow_id": workflow.workflow_id,
                "expeced_cost": estimated_cost,
            },
            status=202,
        )


@api_view(["GET"])
def dehydrate_cache_view(request, key_pattern):
    """
    A simple view to dehydrate cache entries based on a key pattern.
    """
    dehydrate_cache(key_pattern)
    return JsonResponse(
        {"status": "success", "message": "Cache dehydrated successfully."}
    )


class WorkflowConfigView(APIView):
    """
    Class-based view for managing WorkflowConfig.
    """

    def get(self, request):
        """
        Retrieve all WorkflowConfig objects.
        """
        configs = WorkflowConfig.objects.all()
        serializer = WorkflowConfigSerializer(configs, many=True)
        return Response(serializer.data)

    def post(self, request):
        """
        Create a new WorkflowConfig.
        """
        if request.data.get("schema_example") is None:
            return Response(
                {"message": "Schema Example is required!"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        Model, model_string = create_pydantic_model(request.data.get("schema_example"))
        field_names = list(Model.__fields__.keys())
        field_info = list(Model.__fields__.values())

        fields = []

        for i in range(len(field_names)):
            fields.append({field_names[i]: field_info[i].annotation.__name__})

        data = request.data

        data["model_string"] = model_string
        data["fields"] = fields

        serializer = WorkflowConfigSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "message": "Workflow config created successfully!",
                    "config": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, config_id):
        """
        Update an existing WorkflowConfig based on its ID.
        """
        config = get_object_or_404(WorkflowConfig, id=config_id)
        serializer = WorkflowConfigSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, config_id):
        """
        Delete a WorkflowConfig based on its ID.
        """
        config = get_object_or_404(WorkflowConfig, id=config_id)
        config.delete()
        return Response(
            {"message": "Workflow config deleted successfully!"},
            status=status.HTTP_204_NO_CONTENT,
        )


@api_view(["POST"])
def add_user(request):
    serializer = UserSerializer(data=request.data)

    if serializer.is_valid():
        serializer.save()
        return Response(
            {"message": "User created successfully!", "user": serializer.data},
            status=status.HTTP_201_CREATED,
        )
    else:
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TrainModelView(UserIDMixin, CacheDatasetMixin, APIView):

    def post(self, request, *args, **kwargs):
        serializer = ModelDataSerializer(data=request.data)
        user_id = request.META["user"].user_id

        if serializer.is_valid():
            data = serializer.validated_data
            logger.info(f"Training model with data: {data}")
            workflow_id = request.META["workflow_id"]

            training_task = request.data.get("task_type")

            task = Task.objects.create(
                name=f"Training Workflow {workflow_id}",
                status="STARTING",
                workflow_id=workflow_id,
            )

            cached_dataset_id = request.META.get("cached_dataset_id", None)

            train.apply_async(
                args=[data, user_id, training_task, cached_dataset_id],
                task_id=str(task.id),
            )

            return Response(
                {"workflow_id": request.META["workflow_id"], "task_id": task.id},
                status=status.HTTP_202_ACCEPTED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MLModelListView(APIView):

    def get(self, request, format=None):
        models = MLModel.objects.all()
        serializer = MLModelSerializer(models, many=True)
        return Response(serializer.data)


class MLModelDetailView(APIView):

    def get(self, request, model_id, format=None):
        try:
            model = MLModel.objects.get(id=model_id)
            serializer = MLModelSerializer(model)
            return Response(serializer.data)
        except MLModel.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)


class DatasetView(UserIDMixin, CacheDatasetMixin, APIView):

    def get(self, request):
        """
        Fetches CSV files from a Hugging Face dataset repository, with pagination and optional file-specific fetching.

        If 'dataset' is provided, downloads CSV files from the specified Hugging Face dataset (format: {username}/{dataset_name}).
        If 'file' is provided, only this file's data is returned.

        If no Hugging Face dataset is provided, then the dataset generated at autotune is returned, and if no dataset is available,
        HTTP Status No Content is returned.

        Parameters:
         - workflow_id(UUID): The ID of the workflow for which the dataset is to be fetched.
         - page(int): The page number for the dataset - Optional.
         - page_size(int): The number of records per page - Optional.
         - dataset(str): The Hugging Face dataset to be fetched - Optional.
         - file(str): Specific file name to fetch from the dataset - Optional.
        """
        page = request.query_params.get("page", 1)
        page_size = request.query_params.get("perPage", 10)
        file = request.query_params.get("file", None)

        try:
            page = int(page)
            page_size = int(page_size)
        except ValueError:
            return Response(
                {
                    "error": "Page and page size must be integers.",
                    "workflow_id": request.META.get("workflow_id"),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        cached_dataset_id = request.META.get("cached_dataset_id", None)
        data = DatasetData.objects.filter(dataset_id=cached_dataset_id)

        if file:
            data = data.filter(file=file)

        paginated_data, total_count, total_pages = paginate_queryset(
            data, page, page_size
        )

        if not paginated_data:
            return Response(
                {
                    "workflow_id": request.META.get("workflow_id"),
                    "pagination": {
                        "page": page,
                        "perPage": page_size,
                        "totalPages": total_pages,
                        "totalCount": total_count,
                    },
                    "data": [],
                },
                status=status.HTTP_204_NO_CONTENT,
            )

        serializer = DatasetDataSerializer(paginated_data, many=True)
        return Response(
            {
                "workflow_id": request.META.get("workflow_id"),
                "pagination": {
                    "page": page,
                    "perPage": page_size,
                    "totalPages": total_pages,
                    "totalCount": total_count,
                },
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        """
        Gets a dataset from huggingface, and stores it in the local cache if not already not locally cached, and stores any changes in the dataset till it is committed
        to HF just before training is triggered

        Parameters:
         - workflow_id(UUID): The ID of the workflow for which the dataset is to be fetched.
        Body:
         - dataset(str): The Hugging Face dataset. If this is not provided, then fall back to the workflow dataset- Optional.
        """
        input = request.data.get("input", None)
        output = request.data.get("output", None)

        if not input:
            return Response(
                {"error": "Input is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not output:
            return Response(
                {
                    "error": "Output is required.",
                    "workflow_id": request.META.get("workflow_id"),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # will be a valid dataset id, handled in the mixin
        cached_dataset_id = request.META.get("cached_dataset_id", None)
        dataset_object = Dataset.objects.get(id=cached_dataset_id)
        task = dataset_object.type
        task_mapping = get_task_mapping(task)
        keys = list(task_mapping.keys())

        record_data = DatasetData(dataset=dataset_object, file="train.csv")

        setattr(record_data, keys[0], input)
        setattr(record_data, keys[1], output)

        record_data.save()

        return Response(
            {
                "message": "Dataset data saved successfully.",
                "workflow_id": request.META.get("workflow_id"),
            },
            status=status.HTTP_201_CREATED,
        )


class ConfigView(APIView):
    def get(self, request):
        """
        Returns the config of all the tasks or a specific task if provided.

        Args:
            task: task to get the config for -OPTIONAL

        Returns:
            Array of the configs for all the tasks or a single task in an array
        """
        task = request.query_params.get("task", None)
        if task is None:
            return Response({"data": get_task_config()}, status=status.HTTP_200_OK)
        else:
            task_mapping = get_task_mapping(task)
            if task_mapping:
                return Response({"data": task_mapping}, status=status.HTTP_200_OK)
            else:
                return Response(
                    {"error": "Task not found"}, status=status.HTTP_404_NOT_FOUND
                )
