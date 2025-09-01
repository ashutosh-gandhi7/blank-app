import streamlit as st
import json
import os
import copy
from datetime import datetime, timezone
from azure.storage.blob import BlobServiceClient

# --- Configuration ---
# This code now prioritizes an environment variable for the connection string,
# which is the standard practice for self-hosted applications.
AZURE_STORAGE_CONNECTION_STRING = st.secrets["AZURE_STORAGE_CONNECTION_STRING"]
APP_METADATA_CONTAINER_NAME = "app-metadata"

# Check if the connection string was loaded
if not AZURE_STORAGE_CONNECTION_STRING:
    st.error("Azure Storage Connection String is not configured. Please set the AZURE_STORAGE_CONNECTION_STRING environment variable.")
    st.stop()


# Set the page to wide layout. This must be the first Streamlit command.
st.set_page_config(layout="wide")

# --- Azure Blob Storage Functions ---

@st.cache_data(ttl=300) # Cache for 5 minutes
def download_latest_prompt_repo_from_blob():
    """Downloads and parses the latest 'prompt_repo_*.json' blob from Azure."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)

        blob_list = list(container_client.list_blobs(name_starts_with="prompt_repo_"))
        if not blob_list:
            st.warning("No prompt repository found in Azure Blob Storage. Creating a default structure.")
            return {"APPS": [{"name": "mmx", "prompts": []}]}

        latest_blob = max(blob_list, key=lambda b: b.name)
        st.info(f"Loading latest version: {latest_blob.name}")

        blob_client = container_client.get_blob_client(latest_blob.name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load data from Azure Blob Storage: {str(e)}")
        return {"APPS": []}

def upload_prompt_repo_to_blob(data_to_upload: dict):
    """
    Uploads a new timestamped prompt repository to Azure Blob Storage.
    This version DOES NOT delete old blobs.
    """
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        new_blob_name = f"prompt_repo_{timestamp}.json"

        container_client.upload_blob(
            name=new_blob_name,
            data=json.dumps(data_to_upload, indent=4),
            overwrite=True
        )
        st.success(f"Successfully uploaded to Azure as {new_blob_name}")
        return True
    except Exception as e:
        st.error(f"Failed to upload to Azure Blob Storage: {str(e)}")
        return False

def fetch_previous_blobs():
    """Fetches a list of all prompt repository versions from Azure Blob Storage."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        blob_list = list(container_client.list_blobs(name_starts_with="prompt_repo_"))
        blob_list.sort(key=lambda b: b.name, reverse=True)
        return [blob.name for blob in blob_list]
    except Exception as e:
        st.error(f"Failed to fetch previous blobs: {str(e)}")
        return []

def load_blob_content_for_preview(blob_name):
    """Loads and parses the content of a specific blob for previewing."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        blob_client = container_client.get_blob_client(blob_name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load blob {blob_name}: {str(e)}")
        return None

# --- UI Layout ---

st.title("Prompt Repository Editor")

if "prompt_data" not in st.session_state:
    with st.spinner("Loading prompt repository from Azure..."):
        st.session_state.prompt_data = download_latest_prompt_repo_from_blob()

data = st.session_state.prompt_data

if not data or not data.get("APPS"):
    st.error("Could not load a valid prompt structure from Azure. Please check the connection string and container name.")
else:
    st.subheader("Edit Prompt Content")
    prompt_list = data["APPS"][0].get("prompts", [])
    prompt_names = [p.get("name", f"Unnamed Prompt {i}") for i, p in enumerate(prompt_list)]
    if not prompt_names:
        st.warning("No prompts found in the repository.")
    selected_prompt_name = st.selectbox("Select a prompt to edit:", prompt_names)
    selected_index = prompt_names.index(selected_prompt_name) if selected_prompt_name else -1
    initial_content_str = ""
    if selected_index != -1:
        initial_content_str = "\n".join(prompt_list[selected_index].get("content", []))
    edited_content_str = st.text_area("Prompt Content:", value=initial_content_str, height=400, key=f"editor_{selected_prompt_name}")

    if st.button("Upload Changes to Azure"):
        if selected_index != -1 and edited_content_str.strip() != initial_content_str.strip():
            with st.spinner("Uploading to Azure..."):
                updated_data = copy.deepcopy(data)
                updated_data["APPS"][0]["prompts"][selected_index]["content"] = edited_content_str.split('\n')
                if upload_prompt_repo_to_blob(updated_data):
                    st.cache_data.clear()
                    st.session_state.prompt_data = updated_data
                    st.rerun()
        else:
            st.info("No changes detected or no prompt selected.")
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Previous Versions")
        previous_blobs = fetch_previous_blobs()
        if previous_blobs:
            selected_blob = st.selectbox("Select a version to preview", previous_blobs)
            if st.button("Preview Selected Version"):
                with st.spinner(f"Loading preview..."):
                    st.session_state.preview_data = load_blob_content_for_preview(selected_blob)
        if "preview_data" in st.session_state:
            st.subheader("Preview")
            st.json(st.session_state.preview_data, expanded=False)
    with col2:
        st.subheader("Raw JSON Editor")
        edited_raw_json = st.text_area("Edit the full JSON object:", value=json.dumps(data, indent=2), height=450)
        if st.button("Upload Raw JSON to Azure"):
            try:
                new_data = json.loads(edited_raw_json)
                if upload_prompt_repo_to_blob(new_data):
                    st.cache_data.clear()
                    st.session_state.prompt_data = new_data
                    st.rerun()
            except json.JSONDecodeError:
                st.error("Invalid JSON format.")

