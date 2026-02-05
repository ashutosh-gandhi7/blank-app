import streamlit as st
import json
import copy
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from azure.storage.blob import BlobServiceClient
import requests

def get_api_base_url(env):
    """Returns the backend API URL for the selected environment."""
    api_map = {
        "dev": "https://ciathciaidevbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io", 
        "qa": "https://ciathciaitstbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io",
        "prod": "https://ciathciaiprdbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io",
        "aws": "https://ciathena.info:8000"
    }
    return api_map.get(env, api_map["dev"])

def validate_metadata_json(data, target_app_name):
    """
    Validates that the JSON follows the strict structure required by metadata.py.
    Rule 1: Root must be a dictionary.
    Rule 2: Must contain a root key matching the Uppercase App Name (e.g., "MMM").
    Rule 3: The content of that key must be a list of tables.
    """
    if not isinstance(data, dict):
        return False, "❌ Invalid Root: The file must be a JSON object."

    # Logic from metadata.py: json_key = app_name.strip().upper()
    required_key = target_app_name.strip().upper()
    
    if required_key not in data:
        # Check if they used lowercase by mistake to give a helpful error
        found_keys = [k for k in data.keys() if k.lower() == target_app_name.lower()]
        if found_keys:
            return False, f"❌ Casing Error: Found key '{found_keys[0]}', but system requires ALL CAPS: '{required_key}'."
        return False, f"❌ Missing Root Key: JSON must contain the key '{required_key}' at the root."

    # Check if the content is a list (standard metadata structure)
    if not isinstance(data[required_key], list):
         return False, f"❌ Invalid Structure: The value for '{required_key}' must be a list of tables."

    return True, "✅ Valid"


def check_chroma_status(app_name, env):
    """Fetches the current background task status from the backend."""
    base_url = get_api_base_url(env)
    url = f"{base_url}/admin/chroma/status/{app_name}"
    
    try:
        # Simple GET request, usually no auth needed for status, 
        # but you can add the admin header if you secured the GET route too.
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return resp.json()
        return {"status": "unknown", "message": "Could not reach server."}
    except Exception:
        return {"status": "error", "message": "Connection failed."}

def trigger_cache_clear(app_name, env):
    """Calls backend to clear cache."""
    base_url = get_api_base_url(env)
    url = f"{base_url}/admin/cache/clear"
    
    try:
        with st.spinner(f"Clearing cache on {env.upper()}..."):
            resp = requests.post(
                url, 
                json={"app_name": app_name, "target": "all"},
                headers={"x-admin-token": "my-super-secret-admin-key-123"},
                timeout=5
            )
            if resp.status_code == 200:
                st.toast(f"Cache Cleared! ({resp.json().get('keys_deleted')} keys)", icon="✨")
            else:
                st.error(f"Cache Clear Failed: {resp.text}")
    except Exception as e:
        st.error(f"API Error: {str(e)}")

def trigger_chroma_populate(app_name, env):
    """Calls backend to populate ChromaDB."""
    base_url = get_api_base_url(env)
    url = f"{base_url}/admin/chroma/populate"
    
    try:
        with st.spinner(f"🚀 Triggering Chroma Population for {app_name} on {env.upper()}..."):
            # This assumes you added the endpoint described in Part 1
            resp = requests.post(
                url, 
                json={"app_name": app_name},
                headers={"x-admin-token": "my-super-secret-admin-key-123"},
                timeout=5 # fast timeout as it should be a background task
            )
            
            if resp.status_code == 200:
                st.success(f"✅ Job Started: {resp.json().get('message')}")
                st.info("The process is running in the background. It may take a few minutes to complete.")
            else:
                st.error(f"Failed to trigger: {resp.text}")
    except Exception as e:
        st.error(f"API Connection Error: {str(e)}")

# Set the page to wide layout. This must be the first Streamlit command.
st.set_page_config(layout="wide")

# --- 🔐 PASSWORD PROTECTION ---

def check_password():
    """Returns True if the user is logged in, False otherwise."""
    
    # Check if the user is already logged in
    if st.session_state.get("logged_in", False):
        return True

    try:
        # Load the correct password from secrets
        correct_password = st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        st.error("Password is not configured. Please set APP_PASSWORD in Streamlit secrets.")
        st.stop()

    # Create a login form
    with st.form("login"):
        st.header("Login Required")
        st.write("Please enter the password to access the editor.")
        password_attempt = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        if password_attempt == correct_password:
            # If password is correct, set session state and rerun
            st.session_state.logged_in = True
            st.rerun()
        else:
            st.error("Incorrect password. Please try again.")
    
    # If not logged in, return False
    return False

# --- 🏃‍♂️ MAIN APP EXECUTION ---

# Stop execution if the password check fails
if not check_password():
    st.stop()

# --- Configuration & Environment Selection ---

SUPPORTED_APPS = ["mmx", "FAST", "insightsai", "mmm", "patient_claims", "fast1", "insightsai1","ihub","salesmate","access_iq","insights_engine"]
APP_NAME_ALIAS = {
    "fast": "fast1",   # UI fast → backend fast1 (Postgres)
    "fast1": "fast"
}
FORCE_MIGRATION_READ_FROM_ALIAS = {
   
}
ENVIRONMENTS = ["dev", "qa", "prod", "aws"] # Added AWS


def determine_read_app_name(ui_app_name: str) -> str:
    ui_app_name = ui_app_name.lower()

    if FORCE_MIGRATION_READ_FROM_ALIAS.get(ui_app_name, False):
        return APP_NAME_ALIAS.get(ui_app_name, ui_app_name)

    return ui_app_name


def determine_write_app_name(ui_app_name: str) -> str:
    return ui_app_name.lower()


st.sidebar.header("Configuration")
selected_env = st.sidebar.selectbox("Select Environment:", ENVIRONMENTS, index=0)

# Initialize variables
APP_METADATA_CONTAINER_NAME = None
AZURE_STORAGE_CONNECTION_STRING = None
S3_BUCKET_NAME = None
AWS_ACCESS_KEY_ID = None
AWS_SECRET_ACCESS_KEY = None
AWS_REGION = None

# --- Load Secrets based on Environment ---

if selected_env == "aws":
    # --- AWS S3 Configuration ---
    try:
        AWS_ACCESS_KEY_ID = st.secrets["AWS_ACCESS_KEY_ID"]
        AWS_SECRET_ACCESS_KEY = st.secrets["AWS_SECRET_ACCESS_KEY"]
        AWS_REGION = st.secrets["AWS_DEFAULT_REGION"]
        S3_BUCKET_NAME = st.secrets["S3_BUCKET_NAME"]
        st.sidebar.success(f"Target: S3 Bucket '{S3_BUCKET_NAME}'")
    except (KeyError, FileNotFoundError) as e:
        st.error(f"Missing AWS Configuration in secrets: {e}")
        st.stop()
else:
    # --- Azure Blob Configuration ---
    try:
        AZURE_STORAGE_CONNECTION_STRING = st.secrets["AZURE_STORAGE_CONNECTION_STRING"]
        
        # Determine container
        if selected_env == "dev":
            APP_METADATA_CONTAINER_NAME = "app-metadata"
        elif selected_env == "qa":
            APP_METADATA_CONTAINER_NAME = "app-metadata-qa"
        else:
            APP_METADATA_CONTAINER_NAME = "app-metadata-prod"
            
        st.sidebar.info(f"Target: Azure Container '{APP_METADATA_CONTAINER_NAME}'")
        
        if not AZURE_STORAGE_CONNECTION_STRING:
            raise KeyError("AZURE_STORAGE_CONNECTION_STRING is empty")
            
    except (KeyError, FileNotFoundError):
        st.error("Azure Storage Connection String is not configured. Please set it in secrets.")
        st.stop()

# --- Helper Functions ---
def get_blob_prefix(app_name: str) -> str:
    """Gets the correct file prefix based on the application name."""
    app_name_lower = app_name.lower()
    if app_name_lower == "mmx":
        return "prompt_repo_"
    return f"{app_name_lower}_prompt_repo_"

# ==========================================
# --- AWS S3 Functions ---
# ==========================================

def get_s3_client():
    return boto3.client(
        's3',
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION
    )

def get_blob_service_client():
    return BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)

def download_metadata(app_name, env):
    filename = f"{app_name.lower()}.json"
    try:
        if env == "aws":
            s3 = get_s3_client()
            obj = s3.get_object(Bucket=S3_BUCKET_NAME, Key=f"metadata/{filename}") # Assuming structure
            return json.loads(obj['Body'].read().decode('utf-8'))
        else:
            blob_service = get_blob_service_client()
            container = blob_service.get_container_client(APP_METADATA_CONTAINER_NAME)
            blob = container.get_blob_client(filename)
            if not blob.exists():
                return None
            return json.loads(blob.download_blob().readall())
    except Exception as e:
        return None # Return None if not found to handle UI gracefully

def upload_metadata(app_name, data, env):
    filename = f"{app_name.lower()}.json"
    try:
        json_data = json.dumps(data, indent=4)
        if env == "aws":
            s3 = get_s3_client()
            s3.put_object(Bucket=S3_BUCKET_NAME, Key=f"metadata/{filename}", Body=json_data, ContentType='application/json')
        else:
            blob_service = get_blob_service_client()
            container = blob_service.get_container_client(APP_METADATA_CONTAINER_NAME)
            blob = container.get_blob_client(filename)
            blob.upload_blob(json_data, overwrite=True)
        return True
    except Exception as e:
        st.error(f"Upload failed: {e}")
        return False

def download_latest_from_s3(app_name: str):
    """Downloads the latest prompt JSON from S3, with fallback logic matching extract_prompt.py."""
    s3 = get_s3_client()
    prefix = get_blob_prefix(app_name)
    
    try:
        # 1. Try specific app prefix
        response = s3.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix=prefix)
        
        # 2. Fallback logic from extract_prompt.py
        if 'Contents' not in response:
            st.warning(f"No objects found with prefix '{prefix}'. Checking fallback 'prompt_repo_'...")
            fallback_prefix = "prompt_repo_"
            response = s3.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix=fallback_prefix)

        if 'Contents' not in response:
            st.warning(f"No prompt repository found in S3 for '{app_name}'.")
            return {"APPS": [{"name": app_name, "prompts": []}]}

        # Get latest based on Key (lexicographical sort works for timestamped files)
        latest_obj = max(response['Contents'], key=lambda x: x['Key'])
        file_key = latest_obj['Key']
        
        st.info(f"Loading latest S3 version: {file_key}")
        
        obj = s3.get_object(Bucket=S3_BUCKET_NAME, Key=file_key)
        content = obj['Body'].read().decode('utf-8')
        return json.loads(content)

    except Exception as e:
        st.error(f"Failed to load from S3: {str(e)}")
        return {"APPS": []}

def upload_to_s3(app_name: str, data_to_upload: dict):
    """Uploads a new timestamped JSON to S3."""
    s3 = get_s3_client()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    # Consistent naming convention
    if app_name.lower() == "mmx":
        new_key = f"prompt_repo_{timestamp}.json"
    else:
        new_key = f"{app_name.lower()}_prompt_repo_{timestamp}.json"

    try:
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=new_key,
            Body=json.dumps(data_to_upload, indent=4),
            ContentType='application/json'
        )
        st.success(f"Successfully uploaded to S3 as {new_key}")
        return True
    except Exception as e:
        st.error(f"Failed to upload to S3: {str(e)}")
        return False

def fetch_previous_from_s3(app_name: str):
    """Fetches list of file versions from S3."""
    s3 = get_s3_client()
    prefix = get_blob_prefix(app_name)
    
    try:
        response = s3.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix=prefix)
        
        # Handle fallback for history listing as well
        if 'Contents' not in response:
             response = s3.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix="prompt_repo_")

        if 'Contents' in response:
            # Sort descending by key
            sorted_files = sorted(response['Contents'], key=lambda x: x['Key'], reverse=True)
            return [obj['Key'] for obj in sorted_files]
        return []
    except Exception as e:
        st.error(f"Failed to fetch history from S3: {str(e)}")
        return []

def load_s3_preview(key: str):
    """Loads a specific S3 object for preview."""
    s3 = get_s3_client()
    try:
        obj = s3.get_object(Bucket=S3_BUCKET_NAME, Key=key)
        content = obj['Body'].read().decode('utf-8')
        return json.loads(content)
    except Exception as e:
        st.error(f"Failed to load preview {key}: {str(e)}")
        return None

# ==========================================
# --- Azure Blob Functions (Existing) ---
# ==========================================

@st.cache_data(ttl=300)
def download_latest_from_azure(app_name: str):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        prefix = get_blob_prefix(app_name)

        blob_list = list(container_client.list_blobs(name_starts_with=prefix))
        if not blob_list:
            st.warning(f"No prompt repository found for app '{app_name}'.")
            return {"APPS": [{"name": app_name, "prompts": []}]}

        latest_blob = max(blob_list, key=lambda b: b.name)
        st.info(f"Loading latest Azure version: {latest_blob.name}")

        blob_client = container_client.get_blob_client(latest_blob.name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load data from Azure: {str(e)}")
        return {"APPS": []}

def upload_to_azure(app_name: str, data_to_upload: dict):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        
        if app_name.lower() == "mmx":
            new_blob_name = f"prompt_repo_{timestamp}.json"
        else:
            new_blob_name = f"{app_name.lower()}_prompt_repo_{timestamp}.json"

        container_client.upload_blob(
            name=new_blob_name,
            data=json.dumps(data_to_upload, indent=4),
            overwrite=True
        )
        st.success(f"Successfully uploaded to Azure as {new_blob_name}")
        return True
    except Exception as e:
        st.error(f"Failed to upload to Azure: {str(e)}")
        return False

def fetch_previous_from_azure(app_name: str):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        prefix = get_blob_prefix(app_name)
        
        blob_list = list(container_client.list_blobs(name_starts_with=prefix))
        blob_list.sort(key=lambda b: b.name, reverse=True)
        return [blob.name for blob in blob_list]
    except Exception as e:
        st.error(f"Failed to fetch history from Azure: {str(e)}")
        return []

def load_azure_preview(blob_name: str):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        blob_client = container_client.get_blob_client(blob_name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load blob {blob_name}: {str(e)}")
        return None

# ==========================================
# --- 🚦 Main Logic Dispatcher ---
# ==========================================

# These functions decide whether to use S3 or Azure based on `selected_env`

def download_data_dispatcher(app_name, env):
    if env == "aws":
        return download_latest_from_s3(app_name)
    else:
        return download_latest_from_azure(app_name)

def upload_data_dispatcher(app_name, data, env):
    if env == "aws":
        return upload_to_s3(app_name, data)
    else:
        return upload_to_azure(app_name, data)

def fetch_history_dispatcher(app_name, env):
    if env == "aws":
        return fetch_previous_from_s3(app_name)
    else:
        return fetch_previous_from_azure(app_name)

def preview_dispatcher(filename, env):
    if env == "aws":
        return load_s3_preview(filename)
    else:
        return load_azure_preview(filename)

# --- UI Layout ---

st.title("Prompt Repository Editor")

# App selection is the primary driver of the UI
selected_app_name = st.selectbox("Select an App to manage:", SUPPORTED_APPS)

read_app_name = determine_read_app_name(selected_app_name)
write_app_name = determine_write_app_name(selected_app_name)

# When the app changes, clear the cache and reload data
# We also clear if the Environment changes
state_key = f"{read_app_name}_{selected_env}"
if 'current_app_state' not in st.session_state or st.session_state.current_app_state != state_key:
    st.cache_data.clear()
    st.session_state.current_app_state = state_key
    if "preview_data" in st.session_state:
        del st.session_state.preview_data

tab_prompts, tab_metadata = st.tabs(["💬 Prompt Editor", "🗄️ Metadata & Chroma"])
# --- Load Data ---
with tab_prompts:
    with st.spinner(f"Loading data for '{selected_app_name}' from {selected_env.upper()}..."):
        full_data = download_data_dispatcher(read_app_name, selected_env)

    # Find the specific app's data within the loaded structure
    # (Handling case-insensitive matching for app names)
    app_data = None
    if full_data and "APPS" in full_data:
        app_data = next((app for app in full_data["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)

    st.subheader(f"Editing Prompts for: `{selected_app_name}` ({selected_env})")

    st.caption(
        f"📥 Loading from: `{read_app_name}` | "
        f"📤 Saving as: `{write_app_name}`"
    )

    if FORCE_MIGRATION_READ_FROM_ALIAS.get(selected_app_name.lower(), False):
        st.warning("⚠️ Migration mode ON — loading from alias source")

    if app_data is None:
        st.warning(f"Could not find data for '{selected_app_name}' in the loaded file. You can initialize it below.")
        prompt_list = []
    else:
        prompt_list = app_data.get("prompts", [])

    prompt_names = [p.get("name", f"Unnamed Prompt {i}") for i, p in enumerate(prompt_list)]

    # --- Prompt Editor ---
    if not prompt_names:
        st.warning(f"No prompts found for '{selected_app_name}'. You can add one via the Raw JSON Editor.")
    else:
        selected_prompt_name = st.selectbox(
            "Select a prompt to edit:",
            prompt_names,
            key=f"prompt_select_{selected_app_name}"
        )
        selected_prompt_index = prompt_names.index(selected_prompt_name) if selected_prompt_name else -1

        if selected_prompt_index != -1:
            initial_content_str = "\n".join(prompt_list[selected_prompt_index].get("content", []))
            
            edited_content_str = st.text_area(
                "Prompt Content:",
                value=initial_content_str,
                height=400,
                key=f"editor_{selected_app_name}_{selected_prompt_name}"
            )

            if st.button("Upload Changes"):
                if edited_content_str.strip() != initial_content_str.strip():
                    with st.spinner(f"Uploading changes for '{selected_app_name}'..."):
                        updated_data = copy.deepcopy(full_data)
                        
                        # Ensure we have the structure to update
                        if "APPS" not in updated_data:
                            updated_data = {"APPS": [{"name": selected_app_name, "prompts": []}]}

                        # Find the app to update within the copied data structure
                        app_to_update = next((app for app in updated_data["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)
                        
                        if app_to_update:
                            app_to_update["prompts"][selected_prompt_index]["content"] = edited_content_str.split('\n')
                            app_to_update["name"] = write_app_name
                            
                            if upload_data_dispatcher(write_app_name, updated_data, selected_env):
                                trigger_cache_clear(write_app_name, selected_env)
                                st.cache_data.clear()
                                st.rerun()
                        else:
                            st.error(f"Error: Structure mismatch during save.")
                else:
                    st.info("No changes detected.")

    st.divider()

    # --- Raw JSON Editor and Version History ---
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Previous Versions")
        previous_blobs = fetch_history_dispatcher(read_app_name, selected_env)
        
        if previous_blobs:
            selected_blob = st.selectbox("Select a version to preview", previous_blobs, key=f"version_select_{selected_app_name}")
            if st.button("Preview Selected Version"):
                with st.spinner(f"Loading preview for {selected_blob}..."):
                    st.session_state.preview_data = preview_dispatcher(selected_blob, selected_env)
        else:
            st.info(f"No previous versions found for '{selected_app_name}'.")

        if "preview_data" in st.session_state and st.session_state.preview_data:
            st.subheader("Preview")
            st.json(st.session_state.preview_data, expanded=False)

    with col2:
        st.subheader("Raw JSON Editor")
        # Template logic
        json_template = full_data
        # If empty or new, provide a scaffold
        has_app = False
        if full_data and "APPS" in full_data:
            has_app = any(app.get("name", "").lower() == selected_app_name.lower() for app in full_data["APPS"])
        
        if not has_app:
            json_template = {
                "APPS": [
                    {
                        "name": selected_app_name,
                        "prompts": [
                            {
                                "name": "EXAMPLE_PROMPT",
                                "description": "An example description.",
                                "location_identifier": "example.py/my_function()",
                                "content": [
                                    "This is line 1.",
                                    "This is line 2."
                                ]
                            }
                        ]
                    }
                ]
            }
        
        edited_raw_json = st.text_area(
            "Edit the full JSON object for this app:",
            value=json.dumps(json_template, indent=2),
            height=450,
            key=f"raw_json_{selected_app_name}"
        )
        if st.button("Upload Raw JSON"):
            try:
                new_data = json.loads(edited_raw_json)
                # Basic validation
                if "APPS" not in new_data or not isinstance(new_data["APPS"], list):
                    st.error("Invalid JSON structure. Root must contain an 'APPS' list.")
                else:
                    for app in new_data.get("APPS", []):
                        if app.get("name", "").lower() == read_app_name.lower():
                            app["name"] = write_app_name
                    if upload_data_dispatcher(write_app_name, new_data, selected_env):
                        trigger_cache_clear(write_app_name, selected_env)
                        st.cache_data.clear()
                        st.rerun()
            except json.JSONDecodeError:
                st.error("Invalid JSON format. Please correct the syntax.")

with tab_metadata:
    st.subheader(f"Metadata Manager: `{selected_app_name}`")
    
    # 1. Load Metadata
    with st.spinner("Loading metadata..."):
        metadata_json = download_metadata(read_app_name, selected_env)
    
    col_edit, col_act = st.columns([3, 1])
    
    with col_edit:
        st.markdown(f"**File:** `{read_app_name.lower()}.json` in `{APP_METADATA_CONTAINER_NAME if selected_env != 'aws' else S3_BUCKET_NAME}`")
        
        if metadata_json is None:
            st.warning("Metadata file not found. Creating new.")
            json_str = json.dumps({selected_app_name.upper(): []}, indent=4)
        else:
            json_str = json.dumps(metadata_json, indent=4)
            
        edited_meta = st.text_area("Metadata JSON Editor", value=json_str, height=600)
        
        if st.button("💾 Save Metadata"):
            try:
                data_to_save = json.loads(edited_meta)
                is_valid, message = validate_metadata_json(data_to_save, read_app_name)
                if not is_valid:
                    st.error(message) # Block upload if invalid
                else:
                    # 2. Proceed to Upload if Valid
                    if upload_metadata(write_app_name, data_to_save, selected_env):
                        st.success(f"Metadata uploaded successfully! ({message})")
                        trigger_cache_clear(write_app_name, selected_env)
                    else:
                        st.error("Upload failed due to connection error.")
            except json.JSONDecodeError:
                st.error("Invalid JSON Format.")
    
    with col_act:
        st.info("💡 **Chroma Automation**")
        st.markdown("""
        **Steps:**
        1. Edit & Save JSON on the left.
        2. Click button below.
        3. Backend will re-index ChromaDB.
        """)
        
        st.divider()
        
        if st.button("🚀 Start Job", type="primary"):
            trigger_chroma_populate(write_app_name, selected_env)
        
        st.divider()
        
        # --- STATUS DISPLAY ---
        st.markdown("**Job Status**")
        
        # Auto-fetch status on page load (optional) or use a button
        if st.button("🔄 Refresh Status"):
            status_data = check_chroma_status(write_app_name, selected_env)
            
            state = status_data.get("status", "idle")
            msg = status_data.get("message", "")
            time_log = status_data.get("timestamp", "")
            
            if state == "running":
                st.warning(f"🟡 **Running**\n\n{msg}")
            elif state == "completed":
                st.success(f"🟢 **Completed**\n\n{msg}")
                if time_log:
                    st.caption(f"Finished: {time_log}")
            elif state == "failed":
                st.error(f"🔴 **Failed**\n\nError: {msg}")
            else:
                st.info(f"⚪ **Idle**\n\n{msg}")
