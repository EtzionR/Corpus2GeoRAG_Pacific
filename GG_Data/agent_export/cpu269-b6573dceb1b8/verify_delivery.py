"""Standalone hash-pinned public delivery readback. Example: python -B verify_delivery.py SHA256."""
import gzip,hashlib,json,re,sys
from pathlib import Path
DATA_FILES={'graph.json','model/geography.json.gz','model/geometries.parquet','model/events.json.gz','model/relations.json.gz','model/cooccurrence_support.json.gz'}
RUNTIME_FILES={'runtime/geo_agent/__init__.py','runtime/geo_agent/graph_store.py','runtime/geo_agent/model_dates.py',
 'runtime/GG_Data/__init__.py','runtime/GG_Data/pipeline/__init__.py','runtime/GG_Data/pipeline/graph_contract.py',
 'runtime/GG_Data/pipeline/poc.py','runtime/GG_Data/pipeline/relations.py','runtime/GG_Data/pipeline/relation_selection.py','runtime/GG_Data/pipeline/graph_export.py'}
PUBLIC_FILES=DATA_FILES|RUNTIME_FILES|{'verify_delivery.py','LICENSE','requirements.txt','.gitattributes','README.md'}

def sha(path):
 with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def safe_text(value):
 text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
 if re.search(r'(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|/(?:Users|home|mnt|tmp)/|GG_Data[\\/]+pipeline[\\/]+outputs[\\/]|(?:orchestration|publisher|runtime_model_inputs|integration)_direct_\d+)',text):
  raise ValueError('Internal path or development artifact in public content')
 if re.search(r'(?:github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)',text):
  raise ValueError('Credential-like content in public package')

def inspect_public(root):
 import geopandas as gpd
 for name in PUBLIC_FILES:
  path=root/name
  if name.endswith('.parquet'):
   frame=gpd.read_parquet(path)
   if set(frame.columns)!={'place_id','source_dataset','source_object_id','source_url','retrieved_at','geometry'}:
    raise ValueError('Unexpected public GeoParquet columns')
   for col in frame.columns:
    if col!='geometry':safe_text(frame[col].dropna().tolist())
  elif name.endswith('.gz'):
   safe_text(json.loads(gzip.decompress(path.read_bytes())))
  else:safe_text(path.read_text(encoding='utf-8'))

def verify_package(root,manifest_sha256):
 root=Path(root).resolve();manifest_path=root/'delivery_manifest.json'
 if sha(manifest_path)!=manifest_sha256:raise ValueError('Manifest hash differs')
 manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
 if manifest['schema']!='geoai-public-delivery-v1':raise ValueError('Unknown manifest')
 if set(manifest['files'])!=PUBLIC_FILES or set(manifest['data_files'])!=DATA_FILES:raise ValueError('Public allowlist differs')
 actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
 if actual!=PUBLIC_FILES|{'delivery_manifest.json'}:raise ValueError('Unlisted/missing public files')
 for name,digest in manifest['files'].items():
  path=root/name
  if path.is_symlink() or not path.resolve().is_relative_to(root) or sha(path)!=digest:raise ValueError('Unsafe or hash-invalid public artifact: '+name)
 safe_text(manifest);inspect_public(root)
 sys.path.insert(0,str(root/'runtime'))
 from geo_agent.graph_store import GraphStore
 import geo_agent.graph_store as module
 if Path(module.__file__).resolve()!=root/'runtime/geo_agent/graph_store.py':raise ValueError('Non-packaged consumer loaded')
 store=GraphStore.from_json(root/'graph.json',model_dir=root/'model')
 checks=dict(nodes=len(store.nodes),edges=len(store.edges),geometries=len(store.geoms),support_identities=store.schema()['assertion_count'])
 if checks!=manifest['checks']:raise ValueError('Consumer population differs')
 return checks

if __name__=='__main__':
 print(json.dumps(verify_package(Path(__file__).resolve().parent,sys.argv[1])))

