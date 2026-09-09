import p115client
from p115client.util import complete_url as _cu
import logging
_log = logging.getLogger("pipeline")

_orig_fs_rename = p115client.P115Client.fs_rename

def _patched_fs_rename(self, payload, /, base_url="https://webapi.115.com", *, async_=False, **kw):
    api = _cu("/files/batch_rename", base_url=base_url)
    if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], (int, str)):
        params = {f"files_new_name[{payload[0]}]": payload[1]}
    elif isinstance(payload, dict):
        params = {f"files_new_name[{k}]": v for k, v in payload.items()}
    else:
        params = {f"files_new_name[{fid}]": name for fid, name in payload}
    return self.request(url=api, method="GET", payload=params, async_=async_, **kw)

p115client.P115Client.fs_rename = _patched_fs_rename
_log.info("✅ fs_rename 已补丁为 GET 方法")
