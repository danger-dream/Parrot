"""Compatibility-named public facade for the unified :class:`StateStore`.

There is no database connection, SQL, or file path exposed here.  The module
keeps the established domain API while all state ownership and persistence live
in one StateStore instance.
"""
from __future__ import annotations
import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from . import config
from .state_store import StateStore, validate_distinct_paths

_store: StateStore | None = None
_init_lock = threading.Lock()
_migration_report: dict[str, Any] | None = None

def _paths() -> tuple[str, str, str]:
    cfg = config.get()
    old = str(cfg.get("stateDbPath") or "state.db")
    legacy = old if os.path.isabs(old) else os.path.join(config.DATA_DIR, old)
    def resolve(name: str, default: str) -> str:
        value = str(cfg.get(name) or default)
        return value if os.path.isabs(value) else os.path.join(config.DATA_DIR, value)
    return resolve("runtimeStatePath", "runtime-cache.json"), resolve("durableStatePath", "durable-state.json"), legacy

def get_store() -> StateStore:
    if _store is None: raise RuntimeError("state store not started")
    return _store

def _manifest_path(runtime: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(runtime)), "state-migration.json")


def _read_manifest(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) and value.get("version") == 2 else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"[state_store] migration manifest ignored ({path}): {exc}")
        return None


def _write_manifest(path: str, value: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    temp = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
        with open(temp, "r", encoding="utf-8") as handle:
            if json.load(handle) != value: raise RuntimeError("manifest verification mismatch")
        os.replace(temp, path); os.chmod(path, 0o600)
        dfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        try: os.unlink(temp)
        except FileNotFoundError: pass


def migration_report() -> dict[str, Any] | None:
    return json.loads(json.dumps(_migration_report)) if _migration_report else None


def _verified_snapshot_generations(runtime: str, durable: str) -> dict[str, int]:
    verified: dict[str, int] = {}
    for kind, path in (("runtime", runtime), ("durable", durable)):
        generations = []
        for candidate in (path, path + ".bak"):
            try: generations.append(StateStore.read_snapshot(candidate, kind)[0])
            except Exception: pass
        if generations: verified[kind] = max(generations)
    return verified


def init() -> None:
    global _store, _migration_report
    with _init_lock:
        if _store is not None and _store.health()["started"]: return
        runtime, durable, legacy = _paths()
        manifest_path = _manifest_path(runtime)
        artifacts = StateStore.artifact_paths(runtime, durable, manifest_path)
        artifacts.update({"stateDbPath": legacy, "stateDbWal": legacy + "-wal",
                          "stateDbShm": legacy + "-shm", "stateDbJournal": legacy + "-journal"})
        validate_distinct_paths(artifacts)
        for path in (runtime, durable):
            parent = os.path.dirname(os.path.abspath(path)) or "."
            if not os.path.exists(parent): os.makedirs(parent, mode=0o700, exist_ok=True)
            if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
                raise PermissionError(
                    f"state path parent is not writable: {parent}; mount DATA_DIR writable "
                    "or grant the Parrot process access to the custom absolute path")
        store = StateStore(runtime, durable, manifest_path=manifest_path)
        store.start()
        try:
            from .state_migration import LegacyUnavailable, inspect_with_recovery, source_fingerprint
            manifest = _read_manifest(manifest_path)
            verified = _verified_snapshot_generations(runtime, durable)
            try:
                current = source_fingerprint(legacy)
            except LegacyUnavailable as exc:
                if not verified: raise
                _migration_report = {"status": "source-unavailable", "path": legacy, "error": str(exc)}
                print(f"[state_store] legacy source unavailable; retaining verified JSON; path={legacy} error={exc}")
                _store = store
                return
            recorded = (manifest or {}).get("snapshot_generations") or {}
            snapshots_cover_manifest = bool(recorded and all(
                kind in verified and isinstance(generation, int) and verified[kind] >= generation
                for kind, generation in recorded.items()))
            unchanged = bool(manifest and snapshots_cover_manifest and
                             manifest.get("source", {}).get("revision") == current.get("revision"))
            if unchanged:
                _migration_report = {"status": "unchanged", "source": current,
                                     "manifest": manifest_path}
            else:
                report = inspect_with_recovery(legacy, config.DATA_DIR)
                _migration_report = report
                status = report["status"]
                if verified and status in {"backup", "rebuilt-empty"}:
                    # Historical backups are initialization-only. A failed changed
                    # source must remain pending and can never roll verified JSON back.
                    print(f"[state_store] changed legacy source is unusable; retaining verified JSON; "
                          f"path={legacy} reason={report.get('corrupt_reason')}")
                elif verified and status == "missing":
                    value = {"schema": "parrot-state-migration", "version": 2,
                             "source": report["source"], "status": "snapshot-only-missing-source",
                             "snapshot_generations": verified, "completed_at": int(time.time())}
                    _write_manifest(manifest_path, value)
                    print(f"[state_store] legacy source missing; retained verified JSON generations={verified}")
                else:
                    generations = store.install_migration(report["state"])
                    value = {"schema": "parrot-state-migration", "version": 2,
                             "source": report["source"], "status": status,
                             "snapshot_generations": generations, "completed_at": int(time.time())}
                    if report.get("backup"):
                        value["backup"] = report["backup"]
                        value["corrupt_reason"] = report.get("corrupt_reason")
                    _write_manifest(manifest_path, value)
                    print(f"[state_store] installed legacy revision {report['source']['revision'][:12]} "
                          f"status={status} generations={generations}")
            _store = store
        except BaseException:
            store.close()
            raise

def flush(*, strict: bool = False) -> bool: return get_store().flush(strict=strict)
def checkpoint(*, mode: str = "TRUNCATE", strict: bool = False) -> tuple[int, int, int]:
    """Deprecated SQLite-shaped compatibility shim; flush verified JSON."""
    get_store().flush(strict=strict)
    return (0, 0, 0)
def online_backup(destination: str, *, verify: bool = True) -> str:
    raise NotImplementedError(
        "online_backup() retired with SQLite state; copy verified runtime/durable "
        "JSON snapshots and state-migration.json instead")
def health() -> dict[str, Any]: return get_store().health()
def close() -> bool:
    global _store
    if _store is None: return True
    try: return _store.close()
    finally: _store = None

def now_ms() -> int: return int(time.time() * 1000)
@contextmanager
def optional_write_timeout(timeout_ms: int = 100):
    # Historical callers used this before state initialization; keep it optional.
    if _store is None:
        yield
    else:
        with _store.optional_write_timeout(timeout_ms): yield

def _key(*parts: Any) -> str: return "\x1f".join(str(p) for p in parts)
def _get(domain: str, key: str): return get_store().get(domain, key)
def _all(domain: str): return get_store().values(domain)
def _mut(domain: str, fn, *, strict: bool | None=None): return get_store()._mutate(domain, fn, strict=strict)

COMPOSITE_KEY_FLAG = "oauth_composite_key_v1"
COMPOSITE_KEY_VERSION = "1"
def schema_meta_get(key: str) -> str | None:
    row=_get("schema_meta",key); return row.get("value") if row else None
def schema_meta_delete(key:str)->None:_mut("schema_meta",lambda d:d.pop(key,None))
def schema_meta_set(key: str,value: str)->None:
    _mut("schema_meta",lambda d:d.__setitem__(key,{"key":key,"value":value}))
def composite_key_migration_done()->bool:return schema_meta_get("oauth_composite_key_v1")=="1"
def openai_workspace_key_migration_done()->bool:return schema_meta_get("openai_workspace_key_v1")=="1"
def openai_workspace_key_migration_scope_done(scope_key:str)->bool:return schema_meta_get(f"openai_workspace_key_v2:{scope_key}")=="1"

def network_check_save(row:dict[str,Any])->None:
    key=str(row.get("key") or ""); val={"key":key,"label":str(row.get("label") or key),"category":str(row.get("category") or "other"),"ok":1 if row.get("ok") else 0,"detail":str(row.get("detail") or ""),"latency_ms":row.get("latency_ms"),"checked_at":int(row.get("checked_at") or now_ms())}
    _mut("network_check_status",lambda d:d.__setitem__(key,val))
def network_check_load(key:str):return _get("network_check_status",key)
def network_check_load_all():return sorted(_all("network_check_status"),key=lambda r:(r.get("category",""),r.get("key","")))
def network_check_delete(key:str)->None:_mut("network_check_status",lambda d:d.pop(key,None))
def network_check_delete_stale(live_keys:set[str])->None:_mut("network_check_status",lambda d:[d.pop(k,None) for k in list(d) if k not in live_keys])

def xai_video_job_save(request_id:str,*,channel_key:str,api_key_name:str,model:str,ttl_seconds:int)->None:
    ts=now_ms(); exp=ts+max(1,int(ttl_seconds))*1000
    def op(d):
        for k,r in list(d.items()):
            if int(r.get("expires_at") or 0)<=ts:d.pop(k,None)
        d[request_id]={"request_id":request_id,"channel_key":channel_key,"api_key_name":api_key_name,"model":model,"created_at":ts,"expires_at":exp}
    _mut("xai_video_jobs",op,strict=True)
def xai_video_job_load(request_id:str):
    row=_get("xai_video_jobs",request_id)
    if row and int(row.get("expires_at") or 0)>now_ms():return row
    if row:xai_video_job_delete(request_id)
    return None
def xai_video_job_delete(request_id:str|None=None)->None:_mut("xai_video_jobs",lambda d:d.pop(request_id,None) if request_id else d.clear(),strict=True)
def xai_video_job_cleanup(now:int|None=None)->int:
    cutoff=int(now if now is not None else now_ms())
    def op(d):
        keys=[k for k,r in d.items() if int(r.get("expires_at") or 0)<=cutoff]
        for k in keys:d.pop(k,None)
        return len(keys)
    return _mut("xai_video_jobs",op,strict=True)

def perf_save(channel_key:str,model:str,stats:dict[str,Any])->None:
    row={"channel_key":channel_key,"model":model,"total_requests":int(stats.get("total_requests",0)),"success_count":int(stats.get("success_count",0)),"recent_requests":int(stats.get("recent_requests",0)),"recent_success_count":int(stats.get("recent_success_count",0)),"avg_connect_ms":float(stats.get("avg_connect_ms",0)),"avg_first_byte_ms":float(stats.get("avg_first_byte_ms",0)),"avg_total_ms":float(stats.get("avg_total_ms",0)),"last_updated":int(stats.get("last_updated",now_ms()))}
    _mut("performance_stats",lambda d:d.__setitem__(_key(channel_key,model),row))
def perf_load(channel_key:str,model:str):return _get("performance_stats",_key(channel_key,model))
def perf_load_all():return _all("performance_stats")
def perf_delete(channel_key:str|None=None,model:str|None=None)->None:
    _mut("performance_stats",lambda d:[d.pop(k,None) for k,r in list(d.items()) if channel_key is None or (r.get("channel_key")==channel_key and (model is None or r.get("model")==model))])

def _rename_rows(domain:str,old:str,new:str,*,replace_conflicts:bool=False)->None:
    def op(d):
        moving=[(k,r) for k,r in list(d.items()) if r.get("channel_key")==old]
        for k,r in moving:
            nr=dict(r);nr["channel_key"]=new
            nk=_key(new,nr["model"]) if domain in ("performance_stats","channel_errors") else k
            if replace_conflicts or nk not in d:d[nk]=nr
            d.pop(k,None)
    _mut(domain,op)
def perf_rename_channel(old_key:str,new_key:str)->None:
    if old_key!=new_key:_rename_rows("performance_stats",old_key,new_key,replace_conflicts=True)

def error_save(channel_key:str,model:str,error_count:int,cooldown_until:int|None,message:str|None)->None:
    row={"channel_key":channel_key,"model":model,"error_count":error_count,"cooldown_until":cooldown_until,"last_error_message":message,"last_error_at":now_ms()}
    _mut("channel_errors",lambda d:d.__setitem__(_key(channel_key,model),row))
def error_load(channel_key:str,model:str):return _get("channel_errors",_key(channel_key,model))
def error_load_all():return _all("channel_errors")
def error_delete(channel_key:str|None=None,model:str|None=None)->None:
    _mut("channel_errors",lambda d:[d.pop(k,None) for k,r in list(d.items()) if channel_key is None or (r.get("channel_key")==channel_key and (model is None or r.get("model")==model))])
def error_rename_channel(old_key:str,new_key:str)->None:
    if old_key!=new_key:_rename_rows("channel_errors",old_key,new_key,replace_conflicts=True)

def affinity_upsert(fingerprint:str,channel_key:str,model:str,last_used:int|None=None,prompt_cache_key:str|None=None)->None:
    ts=last_used if last_used is not None else now_ms()
    def op(d):
        old=d.get(fingerprint); row=dict(old or {"fingerprint":fingerprint,"created_at":ts,"prompt_cache_key":None});row.update(channel_key=channel_key,model=model,last_used=ts)
        if prompt_cache_key is not None:row["prompt_cache_key"]=prompt_cache_key
        d[fingerprint]=row
    _mut("cache_affinities",op)
def affinity_touch(fingerprint:str,last_used:int|None=None)->None:
    ts=last_used if last_used is not None else now_ms();_mut("cache_affinities",lambda d:d.get(fingerprint,{}).__setitem__("last_used",ts) if fingerprint in d else None)
def affinity_load(fingerprint:str):return _get("cache_affinities",fingerprint)
def affinity_load_all():return _all("cache_affinities")
def affinity_delete(fingerprint:str|None=None)->None:_mut("cache_affinities",lambda d:d.pop(fingerprint,None) if fingerprint else d.clear())
def affinity_delete_by_channel(channel_key:str)->None:_delete_by_channel("cache_affinities",channel_key)
def affinity_delete_stale_channels(live_keys:Iterable[str])->None:_delete_stale("cache_affinities",live_keys)
def affinity_rename_channel(old_key:str,new_key:str)->None:
    if old_key!=new_key:_rename_rows("cache_affinities",old_key,new_key)
def affinity_cleanup(ttl_ms:int,*,cutoff_ms:int|None=None)->int:return _cleanup("cache_affinities",cutoff_ms if cutoff_ms is not None else now_ms()-ttl_ms)

def client_affinity_upsert(client_key:str,channel_key:str,model:str,last_used:int|None=None)->None:
    ts=last_used if last_used is not None else now_ms()
    def op(d):
        row=dict(d.get(client_key) or {"client_key":client_key,"created_at":ts});row.update(channel_key=channel_key,model=model,last_used=ts);d[client_key]=row
    _mut("client_affinities",op)
def client_affinity_load_all():return _all("client_affinities")
def client_affinity_delete(client_key:str|None=None)->None:_mut("client_affinities",lambda d:d.pop(client_key,None) if client_key else d.clear())
def client_affinity_delete_by_channel(channel_key:str)->None:_delete_by_channel("client_affinities",channel_key)
def client_affinity_delete_stale_channels(live_keys:Iterable[str])->None:_delete_stale("client_affinities",live_keys)
def client_affinity_rename_channel(old_key:str,new_key:str)->None:
    if old_key!=new_key:_rename_rows("client_affinities",old_key,new_key)
def client_affinity_cleanup(ttl_ms:int,*,cutoff_ms:int|None=None)->int:return _cleanup("client_affinities",cutoff_ms if cutoff_ms is not None else now_ms()-ttl_ms)
def _delete_by_channel(domain,key):_mut(domain,lambda d:[d.pop(k,None) for k,r in list(d.items()) if r.get("channel_key")==key])
def _delete_stale(domain,live):
    live=set(live);_mut(domain,lambda d:[d.pop(k,None) for k,r in list(d.items()) if r.get("channel_key") not in live])
def _cleanup(domain,cutoff):
    def op(d):
        keys=[k for k,r in d.items() if int(r.get("last_used") or 0)<cutoff]
        for k in keys:d.pop(k,None)
        return len(keys)
    return _mut(domain,op)

def provider_usage_load(account_id:str):return _get("api_provider_usage_cache",account_id)
def provider_usage_load_all():return _all("api_provider_usage_cache")
def provider_usage_set_fetched_at(account_id:str,fetched_at:int)->None:
    _mut("api_provider_usage_cache",lambda d:d.get(account_id,{}).__setitem__("fetched_at",fetched_at) if account_id in d else None)
def provider_usage_save_success(account_id:str,adapter_id:str,snapshot:dict,retry_after:int|None=None)->None:
    row={"account_id":account_id,"adapter_id":adapter_id,"snapshot_json":json.dumps(snapshot,ensure_ascii=False,separators=(",",":")),"fetched_at":now_ms(),"last_error":None,"error_at":None,"retry_after":retry_after};_mut("api_provider_usage_cache",lambda d:d.__setitem__(account_id,row))
def provider_usage_save_error(account_id:str,adapter_id:str,error:str,retry_after:int|None=None)->None:
    def op(d):
        row=dict(d.get(account_id) or {"account_id":account_id,"snapshot_json":None,"fetched_at":None});row.update(adapter_id=adapter_id,last_error=str(error)[:160],error_at=now_ms(),retry_after=retry_after);d[account_id]=row
    _mut("api_provider_usage_cache",op)
def provider_usage_delete(account_id:str)->None:_mut("api_provider_usage_cache",lambda d:d.pop(account_id,None))

_QUOTA_COLUMNS = ("account_key","email","fetched_at","last_passive_update_at","five_hour_util","five_hour_reset","seven_day_util","seven_day_reset","thirty_day_util","thirty_day_reset","sonnet_util","sonnet_reset","opus_util","opus_reset","extra_used","extra_limit","extra_util","raw_data","codex_primary_used_pct","codex_primary_reset_sec","codex_primary_window_min","codex_secondary_used_pct","codex_secondary_reset_sec","codex_secondary_window_min","codex_primary_over_secondary_pct","codex_window_observations")
def _quota_defaults(row:dict[str,Any])->dict[str,Any]:return {column:row.get(column) for column in _QUOTA_COLUMNS}

def _quota_display_email(account_key:str)->str:
    if ":" not in account_key:return account_key
    provider,identity=account_key.split(":",1)
    return identity.split(":",1)[0] if provider in {"openai","xai"} and ":" in identity else identity
def quota_load(account_key_or_email:str):
    exact=_get("oauth_quota_cache",account_key_or_email)
    if exact:return exact
    email=account_key_or_email.split(":",1)[1] if ":" in account_key_or_email else account_key_or_email
    rows=[r for r in _all("oauth_quota_cache") if r.get("email")==email]
    return rows[0] if len(rows)==1 else None
def quota_load_all():return _all("oauth_quota_cache")
def quota_set_observation_times(account_key:str,*,last_passive_update_at:int|None=None,fetched_at:int|None=None,codex_window_observations:str|None=None)->None:
    """Explicit maintenance interface used when correcting observation clocks."""
    def op(d):
        row=d.get(account_key)
        if row is None:return
        if last_passive_update_at is not None:row["last_passive_update_at"]=last_passive_update_at
        if fetched_at is not None:row["fetched_at"]=fetched_at
        if codex_window_observations is not None:row["codex_window_observations"]=codex_window_observations
    _mut("oauth_quota_cache",op)
def _quota_write(account_key:str, operation):
    from . import channel_state
    with channel_state.mutation_lock:
        source=f"oauth:{account_key}"; target=channel_state.resolve(source)
        if channel_state.is_deleted(source) or channel_state.is_deleted(target): return None
        resolved=target[len("oauth:"):] if target.startswith("oauth:") else account_key
        return _mut("oauth_quota_cache",lambda d:operation(d,resolved))

def quota_save(account_key:str,data:dict[str,Any],*,email:str|None=None)->None:
    cols=("five_hour_util","five_hour_reset","seven_day_util","seven_day_reset","thirty_day_util","thirty_day_reset","sonnet_util","sonnet_reset","opus_util","opus_reset","extra_used","extra_limit","extra_util","raw_data")
    def op(d,target):
        row=_quota_defaults(dict(d.get(target) or {}));row.update({"account_key":target,"email":email or _quota_display_email(target),"fetched_at":int(data.get("fetched_at",now_ms()))});row.update({k:data.get(k) for k in cols});d[target]=row
    _quota_write(account_key,op)
def quota_delete(value:str)->None:
    from . import channel_state
    with channel_state.mutation_lock:
        def op(d):
            if ":" in value:
                d.pop(value,None)
                if value.startswith("openai:"):d.pop(value.split(":",1)[1],None)
            else:
                for k,r in list(d.items()):
                    if r.get("email")==value:d.pop(k,None)
        _mut("oauth_quota_cache",op)
def quota_patch_passive(account_key:str,patch:dict,*,email:str|None=None)->None:
    safe={k:v for k,v in patch.items() if k in {"five_hour_util","five_hour_reset","seven_day_util","seven_day_reset"}}
    if not safe:return
    def op(d,target):
        row=_quota_defaults(dict(d.get(target) or {"account_key":target,"email":email or _quota_display_email(target),"fetched_at":0}));row.update(safe);row["last_passive_update_at"]=now_ms();d[target]=row
    _quota_write(account_key,op)
def quota_save_openai_snapshot(account_key:str,snap:dict,normalized:dict|None=None,*,email:str|None=None)->None:
    from .oauth import openai as provider
    if normalized is None:normalized=provider.normalize_codex_snapshot(snap)
    fetched=int(snap.get("fetched_at") or now_ms()); now=int(time.time())
    def reset(sec):return None if sec is None else time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(now+max(0,int(sec))))
    mapping=(("five_hour_util","five_hour_util",False),("five_hour_reset","five_hour_reset_sec",True),("seven_day_util","seven_day_util",False),("seven_day_reset","seven_day_reset_sec",True),("thirty_day_util","thirty_day_util",False),("thirty_day_reset","thirty_day_reset_sec",True))
    raw=(("codex_primary_used_pct","primary_used_pct"),("codex_primary_reset_sec","primary_reset_sec"),("codex_primary_window_min","primary_window_min"),("codex_secondary_used_pct","secondary_used_pct"),("codex_secondary_reset_sec","secondary_reset_sec"),("codex_secondary_window_min","secondary_window_min"),("codex_primary_over_secondary_pct","primary_over_secondary_pct"))
    def op(d,target):
        row=_quota_defaults(dict(d.get(target) or {"account_key":target}));row.update(account_key=target,email=email or _quota_display_email(target),fetched_at=fetched,last_passive_update_at=fetched)
        for col,key,is_reset in mapping:
            if key in normalized:row[col]=reset(normalized.get(key)) if is_reset else normalized.get(key)
        for col,key in raw:row[col]=snap.get(key)
        incoming=provider.codex_snapshot_window_observations(snap)
        if incoming:
            try:existing=json.loads(row.get("codex_window_observations") or "{}")
            except (TypeError,ValueError):existing={}
            merged=provider.merge_codex_window_observations(existing,incoming);row["codex_window_observations"]=json.dumps(merged,ensure_ascii=False,separators=(",",":"),sort_keys=True)
        d[target]=row
    _quota_write(account_key,op)

def quota_rename_account_key(old_key:str,new_key:str,*,email:str|None=None)->int:
    if old_key==new_key:return 0
    from . import channel_state
    with channel_state.mutation_lock:
        if channel_state.is_deleted(f"oauth:{old_key}") or channel_state.is_deleted(f"oauth:{new_key}"): return 0
        def op(d):
            old=d.pop(old_key,None)
            if not old:return 0
            target=dict(d.get(new_key) or {});target.update(old);target["account_key"]=new_key
            if email is not None:target["email"]=email
            d[new_key]=target;return 1
        return _mut("oauth_quota_cache",op)
def run_composite_key_migration(email_to_key:dict[str,str])->dict[str,Any]:
    if composite_key_migration_done():return {"skipped":True,"reason":"flag already set","migrated_quota_rows":0,"migrated_channel_rows":0}
    quota_rows=0;channel_rows=0
    for email,new_account in email_to_key.items():
        quota_rows+=quota_rename_account_key(email,new_account,email=email)
        old_channel=f"oauth:{email}";new_channel=f"oauth:{new_account}"
        before=sum(r.get("channel_key")==old_channel for domain in (perf_load_all(),error_load_all(),affinity_load_all(),client_affinity_load_all()) for r in domain)
        rename_runtime_channel_state(old_channel,new_channel)
        channel_rows+=before
    # Bare-email rows not represented by live config are legacy orphans.
    for row in quota_load_all():
        orphan = str(row.get("account_key") or "")
        if ":" not in orphan and orphan not in email_to_key:
            # Delete the exact storage key. Public quota_delete(email) deliberately
            # retains its email-wide semantics for user-requested cleanup.
            _mut("oauth_quota_cache", lambda d, key=orphan: d.pop(key, None))
    schema_meta_set(COMPOSITE_KEY_FLAG,COMPOSITE_KEY_VERSION)
    return {"skipped":False,"migrated_quota_rows":quota_rows,"migrated_channel_rows":channel_rows}
def run_openai_workspace_key_migration(old_to_new:dict[str,dict[str,str]],*,scope_key:str|None=None)->dict[str,Any]:
    flag=f"openai_workspace_key_v2:{scope_key}" if scope_key else "openai_workspace_key_v1"
    if schema_meta_get(flag)=="1":return {"skipped":True,"reason":"mapping scope already migrated","quota_rows":0,"channel_rows":0}
    quota_rows=0;channel_rows=0
    for old,spec in old_to_new.items():
        new=spec.get("new") or old
        quota_rows+=quota_rename_account_key(old,new,email=spec.get("email"))
        old_channel=f"oauth:{old}";new_channel=f"oauth:{new}"
        before=sum(r.get("channel_key")==old_channel for domain in (perf_load_all(),error_load_all(),affinity_load_all(),client_affinity_load_all()) for r in domain)
        rename_runtime_channel_state(old_channel,new_channel);channel_rows+=before
    schema_meta_set(flag,"1");return {"skipped":False,"quota_rows":quota_rows,"channel_rows":channel_rows}

def rename_runtime_channel_state(old_channel_key:str,new_channel_key:str,*,old_account_key:str|None=None,new_account_key:str|None=None,email:str|None=None)->None:
    domains=("performance_stats","channel_errors","cache_affinities","client_affinities","oauth_quota_cache")
    def op(data):
        if old_channel_key!=new_channel_key:
            for domain in domains[:4]:
                bucket=data[domain]
                for key,row in list(bucket.items()):
                    if row.get("channel_key")!=old_channel_key:continue
                    moved=dict(row);moved["channel_key"]=new_channel_key
                    target=_key(new_channel_key,moved["model"]) if domain in ("performance_stats","channel_errors") else key
                    bucket[target]=moved;bucket.pop(key,None)
        if old_account_key and new_account_key and old_account_key!=new_account_key:
            bucket=data["oauth_quota_cache"];row=bucket.pop(old_account_key,None)
            if row:
                target=dict(bucket.get(new_account_key) or {});target.update(row);target["account_key"]=new_account_key
                if email is not None:target["email"]=email
                bucket[new_account_key]=target
    get_store()._mutate_many(domains,op,strict=False)

def compaction_owner_load(compaction_id:str,content_digest:str):return _get("codex_compaction_owners",_key(compaction_id,content_digest))
def compaction_owner_upsert(compaction_id:str,content_digest:str,owner_key:str,owner_identity:str,*,used_at:int|None=None,compatible_identities:set[str]|None=None)->dict:
    ts=used_at if used_at is not None else now_ms(); key=_key(compaction_id,content_digest)
    def op(d):
        old=d.get(key)
        if old and old.get("owner_identity")!=owner_identity and old.get("owner_identity") not in (compatible_identities or set()):raise ValueError("compaction owner conflict")
        row=dict(old or {"compaction_id":compaction_id,"content_digest":content_digest,"created_at":ts});row.update(owner_key=owner_key,owner_identity=owner_identity,last_used=ts);d[key]=row;return row
    return _mut("codex_compaction_owners",op,strict=True)

# Explicit durable interfaces used by updater/checker/status monitor.
def updater_load()->dict:
    row=_get("app_self_update","1") or {}
    row.pop("id",None); row["stage"]=row.get("stage") or "idle"
    return row
def updater_save(fields:dict[str,Any])->None:
    def op(d):row=dict(d.get("1") or {"id":1});row.update(fields);row["updated_at"]=int(fields.get("updated_at",time.time()));d["1"]=row
    _mut("app_self_update",op,strict=True)
def update_state_load(repo:str):return _get("app_update_state",repo)
def update_state_save(repo:str,row:dict[str,Any])->None:_mut("app_update_state",lambda d:d.__setitem__(repo,{**row,"repo":repo,"checked_at":int(time.time())}),strict=True)
def status_seen_load(provider:str)->set[str]:return {r["update_id"] for r in _all("status_seen_updates") if r.get("provider")==provider}
def status_seen_mark(provider:str,update_id:str,incident_id:str,status:str|None)->None:
    key=_key(provider,update_id);_mut("status_seen_updates",lambda d:d.setdefault(key,{"provider":provider,"update_id":update_id,"incident_id":incident_id,"status":status,"seen_at":int(time.time())}),strict=True)
def status_muted_load_all()->list[dict]:return sorted(_all("status_muted_incidents"),key=lambda r:r.get("muted_at",0),reverse=True)
def status_muted_save(provider:str,incident_id:str,name:str="")->None:
    key=_key(provider,incident_id);_mut("status_muted_incidents",lambda d:d.__setitem__(key,{"provider":provider,"incident_id":incident_id,"name":name or "","muted_at":int(time.time())}),strict=True)
def status_muted_delete(provider:str,incident_id:str)->None:_mut("status_muted_incidents",lambda d:d.pop(_key(provider,incident_id),None),strict=True)
def status_muted_cleanup(live_ids_by_provider:dict[str,set[str]])->int:
    def op(d):
        keys=[k for k,r in d.items() if r.get("provider") in live_ids_by_provider and r.get("incident_id") not in live_ids_by_provider[r["provider"]]]
        for k in keys:d.pop(k,None)
        return len(keys)
    return _mut("status_muted_incidents",op,strict=True)
