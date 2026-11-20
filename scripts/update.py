"""Update a clean Git installation of financial-hot-article from its GitHub main branch."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
ORIGINS = {
    "https://github.com/dhhp2k1/financial-hot-article",
    "git@github.com:dhhp2k1/financial-hot-article",
    "ssh://git@github.com/dhhp2k1/financial-hot-article",
}


def git(*args, timeout=12):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes -oConnectTimeout=8"
    return subprocess.run(["git", *args], cwd=ROOT, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=True).stdout.strip()


def run():
    print("正在检查 GitHub 更新…", flush=True)
    try:
        if Path(git("rev-parse", "--show-toplevel")).resolve() != ROOT:
            print("跳过更新：当前安装目录不是独立 Git 仓库。继续使用当前版本。")
            return
        origin = git("remote", "get-url", "origin").removesuffix(".git").rstrip("/")
        if origin not in ORIGINS:
            print("跳过更新：仓库地址与官方地址不一致。继续使用当前版本。")
            return
        if git("branch", "--show-current") != "main":
            print("跳过更新：当前不在 main 分支。继续使用当前版本。")
            return
        if git("status", "--porcelain", "--untracked-files=normal"):
            print("跳过更新：本地存在未提交的文件或改动，已保留。继续使用当前版本。")
            return
        before = git("rev-parse", "HEAD")
        git("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main", timeout=20)
        remote = git("rev-parse", "refs/remotes/origin/main")
        if before == remote:
            print("更新检查完成：已是最新版本。")
            return
        git("merge-base", "--is-ancestor", before, remote)
        git("merge", "--ff-only", "--no-edit", remote, timeout=20)
        print("更新完成：请重新读取 SKILL.md，再开始本次任务。")
    except FileNotFoundError:
        print("跳过更新：未安装 Git。继续使用当前版本。")
    except subprocess.TimeoutExpired:
        print("更新检查超时。继续使用当前版本。")
    except subprocess.CalledProcessError:
        print("更新未完成：请检查网络、GitHub 仓库访问权限或本地版本状态。继续使用当前版本。")


if __name__ == "__main__":
    run()

