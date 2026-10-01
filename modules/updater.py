import git
import typing
import os
import atexit
import sys
import subprocess

from git.exc import GitCommandError
from git import Repo

REPO_URL = "https://github.com/MainMask/teleharvester"


def _init_repo() -> Repo:
    """Initialise a local repo tracking teleharvester's master, for update checks."""
    repo = Repo.init(os.getcwd())
    origin = repo.create_remote("origin", REPO_URL)
    origin.fetch()
    repo.create_head("master", origin.refs.master)
    repo.heads.master.set_tracking_branch(origin.refs.master)
    repo.heads.master.checkout(True)
    return repo


def get_current_commit() -> typing.Union[bool, str]:
    """Get current commit"""

    try:
        repo = git.Repo()
        return repo.head.commit.hexsha
    except Exception:
        return False


def check_update() -> dict:
    """Check update for teleharvester"""

    try:
        repo = git.Repo(os.getcwd())
    except git.exc.GitError:
        try:
            repo = _init_repo()
        except Exception as err:
            print(f"Warning: could not initialize repo for updates: {err}")
            return {"has_update": False}

    try:
        fetch_infos = git.Remote(repo, "origin").fetch()

        if not fetch_infos:
            return {"has_update": False}

        upcoming_commit = repo.remotes.origin.refs.master.commit
    except GitCommandError as err:
        if "detected dubious ownership" in (err.stderr or ""):
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", os.getcwd()],
                check=True,
            )
            return check_update()

        else:
            print(f"Warning: could not check for updates: {err}")
            return {"has_update": False}
    except Exception as err:
        print(f"Warning: could not check for updates: {err}")
        return {"has_update": False}

    current_commit = get_current_commit()

    if current_commit == upcoming_commit.hexsha:
        return {"has_update": False}

    return {
        "has_update": True,
        "current_commit": current_commit,
        "upcoming_commit": upcoming_commit.hexsha,
        "message": upcoming_commit.message
    }


def update_requirements(console):
    with console.status("Installing new requirements..."):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                os.path.join(
                    os.getcwd(),
                    "requirements.txt",
                ),
                "--user",
            ],
            check=True,
        )

    console.print("[bold green]New requirements installed successfully.")


def on_exit():
    os.execl(
        sys.executable,
        sys.executable,
        *sys.argv
    )


def restart_app():
    atexit.register(on_exit)
    exit(0)


def update(console):
    try:
        with console.status("Updating..."):
            repo = Repo(os.getcwd())
            origin = repo.remote("origin")
            r = origin.pull()
        
        console.print("[bold green]Updated successfully!")

        new_commit = repo.head.commit

        for info in r:
            if not info.old_commit:
                continue

            for d in new_commit.diff(info.old_commit):
                if d.b_path == "requirements.txt":
                    update_requirements(console)
        
        restart_app()
    except git.exc.InvalidGitRepositoryError:
        repo = _init_repo()
