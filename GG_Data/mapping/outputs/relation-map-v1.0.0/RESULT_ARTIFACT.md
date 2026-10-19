# Complete mapped relations result

The final CSV is stored as `complete_mapped_relations_v1.0.0.csv.gz` because GitHub blocks new Git LFS objects in public forks.

- Uncompressed file: `complete_mapped_relations_v1.0.0.csv`
- Uncompressed size: `678290241` bytes
- Uncompressed SHA-256: `e1492d175577b5b4b916c9e12dbc0ff61b50a0f86e23c3471e5c033e0b85c320`
- Compressed size: `60222999` bytes
- Compressed SHA-256: `4ad3a8a8c5d4525479c017aaf7e872637bfbc787f9d8b3ce7d917d5f94ab9fe6`

Extract the CSV with Python:

```python
import gzip
import shutil

with gzip.open("complete_mapped_relations_v1.0.0.csv.gz", "rb") as source:
    with open("complete_mapped_relations_v1.0.0.csv", "wb") as target:
        shutil.copyfileobj(source, target)
```

The mapping notebook can also regenerate the uncompressed CSV from the raw sources.
