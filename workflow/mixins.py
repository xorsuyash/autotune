import logging
import traceback
import uuid

import pandas as pd
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from huggingface_hub import HfApi
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from workflow.models import User

from .models import Dataset, DatasetData, Workflows
from .utils import get_task_mapping

logger = logging.getLogger(__name__)


class LoggingMixin:
    """
    Provides full logging of requests
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger("django.request")

    def initial(self, request, *args, **kwargs):
        try:
            extra = {
                "request": request.data,
                "method": request.method,
                "endpoint": request.path,
                "user": request.user.username,
                "ip_address": request.META.get("REMOTE_ADDR"),
                "user_agent": request.META.get("HTTP_USER_AGENT"),
                "headers": dict(request.headers),
            }
            self.logger.info(f"Request received: {extra}")
        except Exception:
            self.logger.exception("Error logging request data")

        super().initial(request, *args, **kwargs)


class UserIDMixin:
    def dispatch(self, request, *args, **kwargs):
        user_id = request.headers.get("User-Id")
        if not user_id:
            return JsonResponse({"error": "User ID must be provided."}, status=401)

        try:
            user_id = uuid.UUID(user_id)
        except ValueError:
            return JsonResponse({"error": "Invalid user ID format."}, status=401)

        role = request.headers.get("role")

        if not role:
            return JsonResponse({"error": "Role must be provided."}, status=401)

        user = cache.get(f"user_{user_id}")
        if not user:
            try:
                user = User.objects.get(user_id=user_id)
                cache.set(f"user_{user_id}", user, timeout=3600)  # Cache for 1hr
            except User.DoesNotExist:
                logger.info(
                    f"User {user_id} not found in the database. Creating new user."
                )
                user = User.objects.create(
                    user_id=user_id,
                    role=role,
                    is_active=True,
                )
                cache.set(f"user_{user_id}", user, timeout=3600)
        else:
            logger.info(f"User {user_id} found in cache.")

        request.META["user"] = user
        return super().dispatch(request, *args, **kwargs)


def is_valid_uuid(uuid_to_test, version=4):
    """
    Check if uuid_to_test is a valid UUID.
    """
    try:
        uuid_obj = uuid.UUID(str(uuid_to_test), version=version)
        return str(uuid_obj) == str(uuid_to_test)
    except ValueError:
        return False


class CacheDatasetMixin:
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        """
        Mixin for all endpoints which deal with a dataset. it has to be preceded by a datasetId in the request endpoint.
        This mixin will check if the dataset is already in the cache, if not it will download the dataset from huggingface and cache it.
        Needs to either have a dataset associated with the workflow_id in autotune or provide a dataset and type in the request.
        """

        try:
            user_id = request.META["user"].user_id
            dataset = None

            workflows = Workflows.objects.filter(user_id=user_id)

            if request.method == "GET":
                dataset = request.GET.get("dataset")
                task_type = request.GET.get("task_type")
            elif request.method == "POST":
                dataset = request.POST.get("dataset")
                task_type = request.POST.get("task_type")

            logger.info(f"recieved dataset {dataset} and task_type {task_type}")

            workflow_id = None
            created = False

            task_mapping = get_task_mapping(task_type)

            if not task_mapping:
                raise ValueError("Please give a valid task type.")

            for workflow in workflows:
                print("in the for loop")
                print(workflow)
                test_dataset = Dataset.objects.filter(
                    workflow_id=workflow.workflow_id
                ).first()
                if test_dataset and test_dataset.type == task_type:
                    workflow_id = workflow.workflow_id
                    logger.info(
                        "found an existing workflow with this task for the given user"
                    )
                    break

            print(workflow_id)

            # Create workflow for a user for given task if no workflow w a dataset of given task exists
            if workflow_id is None:
                created_workflow = Workflows.objects.create(
                    user_id=user_id,
                    type="Training",
                    workflow_name=f"training_{task_type}",
                )
                workflow_id = created_workflow.workflow_id
                created = True
                logger.info(
                    f"created a workflow for the task with workflow_id {workflow_id}"
                )

            # When user gives a HF dataset to use for caching the data
            if dataset:
                huggingface_id, dataset_name = dataset.split("/")
                dataset_object = Dataset.objects.filter(
                    type=task_type,
                    workflow_id=workflow_id,
                ).first()

                # No existing dataset objectwith the given task
                if not dataset_object:
                    dataset_object = Dataset.objects.create(
                        huggingface_id=huggingface_id,
                        name=dataset_name,
                        type=task_type,
                        workflow_id=workflow_id,
                        is_locally_cached=False,
                    )

                # data not in cache
                if not dataset_object.is_locally_cached:
                    self.cache_dataset(dataset_object, task_mapping, dataset)

            # Dataset generated at autotune and user wants to use that.
            elif not created:
                dataset_object = Dataset.objects.filter(workflow_id=workflow_id).first()
                if not dataset_object:
                    raise ValueError("No dataset associated with the workflow.")

                if not dataset_object.is_locally_cached:
                    self.cache_dataset(
                        dataset_object,
                        task_mapping,
                        f"{dataset_object.huggingface_id}/{dataset_object.name}",
                    )

            else:
                raise ValueError("No dataset available.")

            request.META["cached_dataset_id"] = dataset_object.id
            request.META["workflow_id"] = workflow_id

            response = super().dispatch(request, *args, **kwargs)

            print("Request pre-processing completed.")

            return response

        except ValueError as ve:
            logger.error(f"\n{traceback.format_exc()}")
            response = Response({"error": str(ve)}, status=status.HTTP_400_BAD_REQUEST)
        except FileNotFoundError as fnfe:
            logger.error(f"\n{traceback.format_exc()}")
            response = Response({"error": str(fnfe)}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"\n{traceback.format_exc()}")
            response = Response(
                {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        response.accepted_renderer = JSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {}
        return response

    def cache_dataset(self, dataset_object, task_mapping, dataset):
        logger.info("didnt find the dataset in the cache, creating a new cache")
        hf_api = HfApi(token=settings.HUGGING_FACE_TOKEN)

        if hf_api.repo_exists(repo_id=dataset, repo_type="dataset"):
            repo_info = hf_api.repo_info(repo_id=dataset, repo_type="dataset")
            repo_files = hf_api.list_repo_files(repo_id=dataset, repo_type="dataset")
            csv_file_names = [f for f in repo_files if f.endswith(".csv")]

        else:
            raise FileNotFoundError("Dataset not found.")

        csv_files = []

        for csv_file_name in csv_file_names:
            csv_files.append(
                hf_api.hf_hub_download(
                    repo_id=dataset,
                    repo_type="dataset",
                    filename=csv_file_name,
                )
            )

        if not csv_files:
            raise ValueError("Dataset doesn't have any CSV files.")

        for csv_file in csv_files:
            df = pd.read_csv(csv_file)

            for csv_column in task_mapping.values():
                if csv_column not in df.columns:
                    raise ValueError(
                        f"Column '{csv_column}' does not exist in the dataset for {csv_file.split('/')[-1]}"
                    )

            if "id" in df.columns:
                # Check and replace non-UUID values with UUIDs
                df["id"] = df["id"].apply(
                    lambda x: (str(uuid.uuid4()) if not is_valid_uuid(x) else x)
                )
            else:
                # Add an 'id' column with new UUIDs
                df["id"] = [str(uuid.uuid4()) for _ in range(len(df))]

            filename = csv_file.split("/")[-1]
            for _, row in df.iterrows():
                row_data = DatasetData(
                    id=uuid.UUID(row["id"]),
                    dataset=dataset_object,
                    file=filename,
                )

                # get the appropriate columns and their mapping.
                for task_key, csv_column in task_mapping.items():
                    setattr(row_data, task_key, row[csv_column])

                row_data.save()
            logger.info(f"inserted all record into the db for {filename}")

        dataset_object.is_locally_cached = True
        dataset_object.latest_commit_hash = repo_info.sha
        dataset_object.save()
