import copy
from threading import Lock
from datetime import datetime, timezone, timedelta
from litellm.integrations.custom_logger import CustomLogger

print("[HOOK-PATCH] hooks.py is being imported!", flush=True)

# Import target modules for optimization caching
try:
    from litellm.llms.sap.chat.transformation import GenAIHubOrchestrationConfig
    import litellm.llms.sap.credentials as sap_credentials
    import litellm.llms.sap.chat.transformation as sap_chat_transformation
    import litellm.llms.sap.embed.transformation as sap_embed_transformation

    # 1. Patch deployment_url to cache it globally (avoids fetching deployments/configurations on every call)
    original_deployment_url = GenAIHubOrchestrationConfig.deployment_url
    _cached_dep_url = None
    _dep_url_lock = Lock()

    def patched_deployment_url(self):
        global _cached_dep_url
        print(f"[HOOK-PATCH] patched_deployment_url called. Current cached url: {_cached_dep_url}", flush=True)
        if _cached_dep_url is None:
            with _dep_url_lock:
                if _cached_dep_url is None:
                    print("[HOOK-PATCH] Resolving deployment URL from upstream (first time)...", flush=True)
                    _cached_dep_url = original_deployment_url.__get__(self, GenAIHubOrchestrationConfig)
                    print(f"[HOOK-PATCH] Resolved deployment URL: {_cached_dep_url}", flush=True)
        return _cached_dep_url

    GenAIHubOrchestrationConfig.deployment_url = property(patched_deployment_url)

    # 2. Patch get_token_creator to cache tokens globally (avoids requesting new OAuth token on every call)
    original_get_token_creator = sap_credentials.get_token_creator
    _cached_token = None
    _cached_token_expiry = None
    _token_lock = Lock()

    def patched_get_token_creator(*args, **kwargs):
        print("[HOOK-PATCH] patched_get_token_creator called.", flush=True)
        original_get, base_url, resource_group = original_get_token_creator(*args, **kwargs)
        
        def cached_get_token():
            global _cached_token, _cached_token_expiry
            now = datetime.now(timezone.utc)
            print(f"[HOOK-PATCH] cached_get_token called. Current token cached: {bool(_cached_token)}, Expiry: {_cached_token_expiry}", flush=True)
            if _cached_token is None or _cached_token_expiry is None or _cached_token_expiry - now < timedelta(minutes=5):
                with _token_lock:
                    now = datetime.now(timezone.utc)
                    if _cached_token is None or _cached_token_expiry is None or _cached_token_expiry - now < timedelta(minutes=5):
                        print("[HOOK-PATCH] Requesting new OAuth token from upstream...", flush=True)
                        _cached_token = original_get()
                        _cached_token_expiry = now + timedelta(minutes=50)
            return _cached_token
            
        return cached_get_token, base_url, resource_group

    sap_credentials.get_token_creator = patched_get_token_creator
    sap_chat_transformation.get_token_creator = patched_get_token_creator
    sap_embed_transformation.get_token_creator = patched_get_token_creator

except Exception as e:
    # Fallback gracefully if library imports change in future versions
    import logging
    logging.warning(f"Unable to apply SAP performance optimization patches: {e}")


class SAPPerformanceOptimizer(CustomLogger):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        print("[HOOK-PATCH] SAPPerformanceOptimizer class instantiated! Performance patches are active.", flush=True)

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type,
    ):
        print(f"[HOOK-PATCH] SAPPerformanceOptimizer async_pre_call_hook called with call_type: {call_type}", flush=True)
        
        # Force stable mode (no streaming)
        data["stream"] = False
        
        # Remove problematic fields
        remove_fields = [
            "stream_options",
            "thinking",
            "logprobs",
            "output_config",
        ]
        for field in remove_fields:
            data.pop(field, None)
            
        print(f"[HOOK-PATCH] Sanitized request data successfully: {data}", flush=True)
        return data


# Instantiate the class to provide a singleton object to LiteLLM callbacks
sap_perf_optimizer = SAPPerformanceOptimizer()


def sanitize_request(**kwargs):
    # Backward compatibility wrapper
    data = kwargs.get("data", {})
    if not data:
        return kwargs
    data = copy.deepcopy(data)
    
    # FORCE STABLE MODE
    data["stream"] = False
    kwargs["stream"] = False

    # REMOVE PROBLEMATIC FIELDS
    remove_fields = [
        "stream_options",
        "thinking",
        "logprobs",
        "output_config",
    ]
    for field in remove_fields:
        data.pop(field, None)
        kwargs.pop(field, None)

    kwargs["data"] = data
    return kwargs