import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas

logger = logging.getLogger(__name__)


def upload_filled_application(file_path: str, download_name: str) -> str | None:
    """
    Upload a filled application DOCX to Azure Blob Storage and return a SAS URL valid for 1 hour.

    Args:
        file_path: Path to the DOCX file on disk
        download_name: Filename to use in the download (e.g., "DHCFS_Application_Filled.docx")

    Returns:
        Public SAS URL for downloading the file, or None if upload fails
    """
    try:
        connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if not connection_string:
            logger.error("AZURE_STORAGE_CONNECTION_STRING environment variable not set")
            return None

        blob_service = BlobServiceClient.from_connection_string(connection_string)
        container_name = "applications"

        try:
            blob_service.create_container(container_name)
            logger.info("Created blob container: %s", container_name)
        except ResourceExistsError:
            pass

        blob_name = f"{uuid.uuid4().hex}_{download_name}"
        blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)

        with open(file_path, "rb") as handle:
            blob_client.upload_blob(handle, overwrite=True)

        logger.info("Uploaded %s to blob storage", download_name)

        sas_token = generate_blob_sas(
            account_name=blob_client.account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=blob_service.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        download_url = f"{blob_client.url}?{sas_token}"
        logger.info("Generated SAS URL for %s", download_name)
        return download_url

    except Exception as exc:
        logger.error("Blob upload failed: %s", exc, exc_info=True)
        return None
