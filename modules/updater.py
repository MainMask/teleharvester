import git
import typing
import os
import atexit
import sys
import subprocess

from git.exc import GitCommandError
from git import Repo

# Never git-init + force-checkout a non-git install dir: that silently replaces the
# local code (e.g. an rsync/tarball deploy) with GitHub's master.
NOT_A_CHECKOUT = "Не git-репозиторий — проверка обновлений пропущена."


def get_current_commit() -> typing.Union[bool, str]:
    """Get current commit"""

    try:
        repo = git.Repo()
        return repo.head.commit.hexsha
    except Exception:
        return False


def check_update(retried: bool = False) -> dict:
    """Check update for teleharvester"""

    try:
        repo = git.Repo(os.getcwd())
    except git.exc.GitError:
        print(NOT_A_CHECKOUT)
        return {"has_update": False}

    try:
        fetch_infos = git.Remote(repo, "origin").fetch()

        if not fetch_infos:
            return {"has_update": False}

        upcoming_commit = repo.remotes.origin.refs.master.commit
    except GitCommandError as err:
        # once: if git still refuses, each retry would only repeat the line in ~/.gitconfig
        if "detected dubious ownership" in (err.stderr or "") and not retried:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", os.getcwd()],
                check=True,
            )
            return check_update(retried=True)

        else:
            print(f"Внимание: не удалось проверить обновления: {err}")
            return {"has_update": False}
    except Exception as err:
        print(f"Внимание: не удалось проверить обновления: {err}")
        return {"has_update": False}

    current_commit = get_current_commit()

    # origin/master already contained in HEAD (e.g. local commits ahead): nothing to pull
    if current_commit == upcoming_commit.hexsha or repo.is_ancestor(upcoming_commit, repo.head.commit):
        return {"has_update": False}

    return {
        "has_update": True,
        "current_commit": current_commit,
        "upcoming_commit": upcoming_commit.hexsha,
        "message": upcoming_commit.message
    }


def update_requirements(console):
    args = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        os.path.join(
            os.getcwd(),
            "requirements.txt",
        ),
    ]

    if sys.prefix == sys.base_prefix:  # pip refuses --user inside a venv
        args.append("--user")

    with console.status("Установка зависимостей..."):
        subprocess.run(args, check=True)

    console.print("[bold green]Зависимости установлены.")


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
        with console.status("Обновление..."):
            repo = Repo(os.getcwd())
            old_commit = repo.head.commit
            origin = repo.remote("origin")
            origin.pull()
        
        console.print("[bold green]Обновлено!")

        # diff the HEAD move itself: check_update() already fetched, so pull()'s own
        # FetchInfo reports origin as up to date and carries no old_commit
        if any(d.b_path == "requirements.txt" for d in old_commit.diff(repo.head.commit)):
            update_requirements(console)
        
        restart_app()
    except git.exc.InvalidGitRepositoryError:
        console.print(NOT_A_CHECKOUT)
    except GitCommandError as err:  # local changes / diverged branch: keep running the current code
        console.print(f"[bold red]Обновление не удалось:[/] {err}")
    except subprocess.CalledProcessError as err:  # no restart: the new code would miss its deps
        console.print(
            f"[bold red]Не удалось установить зависимости:[/] {err}. "
            "Выполните `pip install -r requirements.txt` и перезапустите."
        )
