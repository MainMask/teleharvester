"""Import the tdata_import/<name>/tdata folders into a project's workers: the terminal menu's
«📂 tdata из папки» (functions/sign_in.py)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT)
os.chdir(ROOT)  # tdata_import/, projects/ are relative to the root

from functions.sign_in import TdataImportFunc, run_standalone

if __name__ == "__main__":
    run_standalone(TdataImportFunc)
