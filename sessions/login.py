"""An account's login codes from the last minutes: the terminal menu's «Код входа»
(functions/sign_in.py). With a session file — a worker's or a personal one — its codes at once."""
import os
import sys

if len(sys.argv) > 2:
    print("Использование: python login.py [файл_сессии]")
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the file as given from the cwd, seen from the root
path = os.path.relpath(os.path.abspath(sys.argv[1]), ROOT) if len(sys.argv) == 2 else None
sys.path.append(ROOT)
os.chdir(ROOT)  # sessions/, personal_sessions/ are relative to the root

from functions.sign_in import LoginCodesFunc, run_standalone

if __name__ == "__main__":
    run_standalone(LoginCodesFunc, *([path] if path else []))
