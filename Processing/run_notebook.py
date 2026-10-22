"""Execute a notebook in place (outputs saved), e.g.  python Processing/run_notebook.py Processing/crawl_corpus.ipynb"""
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

path = Path(sys.argv[1]).resolve()
nb = nbformat.read(path, as_version=4)
NotebookClient(nb, timeout=None, kernel_name="python3", resources={"metadata": {"path": str(path.parent)}}).execute()
nbformat.write(nb, path)
print(f"executed {path.name}")
