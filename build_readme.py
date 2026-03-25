import asyncio
import httpx
import pathlib
import re
import os
from python_graphql_client import GraphqlClient

root = pathlib.Path(__file__).parent.resolve()
client = GraphqlClient(endpoint="https://api.github.com/graphql")

TOKEN = os.environ.get("RMDES_TOKEN", "")

NPM_MAINTAINER = "rmdes"
NPM_SCOPE = "@rmdes/"


def replace_chunk(content, marker, chunk, inline=False):
    r = re.compile(
        r"<!\-\- {} starts \-\->.*<!\-\- {} ends \-\->".format(marker, marker),
        re.DOTALL,
    )
    if not inline:
        chunk = "\n{}\n".format(chunk)
    chunk = "<!-- {} starts -->{}<!-- {} ends -->".format(marker, chunk, marker)
    return r.sub(chunk, content)


# --- Recently Active Repos (sorted by last push) ---

GRAPHQL_ACTIVE_REPOS_QUERY = """
query {
  user(login: "rmdes") {
    repositories(first: 10, privacy: PUBLIC, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name
        url
        description
        pushedAt
        defaultBranchRef {
          target {
            ... on Commit {
              messageHeadline
              committedDate
            }
          }
        }
      }
    }
  }
}
"""


def fetch_active_repos(oauth_token):
    data = client.execute(
        query=GRAPHQL_ACTIVE_REPOS_QUERY,
        headers={"Authorization": "Bearer {}".format(oauth_token)},
    )
    repos = data["data"]["user"]["repositories"]["nodes"]
    # Skip this profile repo itself
    return [r for r in repos if r["name"] != "rmdes"]


# --- Recently Starred Repos ---

GRAPHQL_STARS_QUERY = """
query {
  user(login: "rmdes") {
    starredRepositories(first: 8, orderBy: {field: STARRED_AT, direction: DESC}) {
      nodes {
        nameWithOwner
        url
        description
        stargazerCount
      }
    }
  }
}
"""


def fetch_starred(oauth_token):
    data = client.execute(
        query=GRAPHQL_STARS_QUERY,
        headers={"Authorization": "Bearer {}".format(oauth_token)},
    )
    return data["data"]["user"]["starredRepositories"]["nodes"]


# --- Blog Posts (JSON Feed) ---


def fetch_blog_posts():
    response = httpx.get("https://rmendes.net/feed.json", timeout=30)
    items = response.json().get("items", [])
    posts = []
    for item in items:
        title = item.get("title")
        if not title:
            continue
        url = item.get("url", "")
        # Skip replies — only keep articles, bookmarks, reposts with titles
        if "/replies/" in url:
            continue
        date = item.get("date_published", "")[:10]
        posts.append({"title": title, "url": url, "published": date})
        if len(posts) >= 8:
            break
    return posts


# --- npm Download Stats ---


def discover_npm_packages():
    """Discover all @rmdes/* packages from the npm registry."""
    resp = httpx.get(
        "https://registry.npmjs.org/-/v1/search",
        params={"text": f"maintainer:{NPM_MAINTAINER}", "size": 250},
        timeout=30,
    )
    if resp.status_code != 200:
        return []
    return [
        p["package"]["name"]
        for p in resp.json().get("objects", [])
        if p["package"]["name"].startswith(NPM_SCOPE)
    ]


async def fetch_npm_downloads(package_names):
    """Fetch monthly download counts for all discovered packages."""
    sem = asyncio.Semaphore(5)

    async def fetch_one(http, pkg):
        async with sem:
            return await http.get(
                f"https://api.npmjs.org/downloads/point/last-month/{pkg}"
            )

    async with httpx.AsyncClient(timeout=30) as http:
        tasks = [fetch_one(http, pkg) for pkg in package_names]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    packages = []
    total = 0
    for resp in responses:
        if isinstance(resp, Exception):
            continue
        if resp.status_code != 200:
            continue
        data = resp.json()
        downloads = data.get("downloads", 0)
        name = data.get("package", "")
        if downloads > 0:
            packages.append({"name": name, "downloads": downloads})
            total += downloads

    packages.sort(key=lambda p: p["downloads"], reverse=True)
    return packages, total


# --- CI/CD Pipeline Status ---

PIPELINES = [
    {"repo": "rmdes/indiekit-cloudron", "label": "Cloudron", "description": "Production deployment at rmendes.net"},
    {"repo": "rmdes/indiekit-deploy", "label": "Docker Compose", "description": "Standalone server deployment"},
]


def fetch_pipeline_status(oauth_token):
    """Fetch latest workflow run for each pipeline."""
    pipelines = []
    for pipe in PIPELINES:
        resp = httpx.get(
            f"https://api.github.com/repos/{pipe['repo']}/actions/runs",
            params={"per_page": 1},
            headers={"Authorization": f"Bearer {oauth_token}"},
            timeout=30,
        )
        if resp.status_code != 200:
            continue
        runs = resp.json().get("workflow_runs", [])
        if not runs:
            continue
        run = runs[0]
        # Compute build duration
        created = run.get("run_started_at") or run.get("created_at", "")
        updated = run.get("updated_at", "")
        duration = ""
        if created and updated and run["status"] == "completed":
            from datetime import datetime
            t0 = datetime.fromisoformat(created.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            secs = int((t1 - t0).total_seconds())
            if secs >= 60:
                duration = f"{secs // 60}m {secs % 60}s"
            else:
                duration = f"{secs}s"

        conclusion = run.get("conclusion") or run.get("status", "unknown")
        pipelines.append({
            "label": pipe["label"],
            "description": pipe["description"],
            "repo": pipe["repo"],
            "conclusion": conclusion,
            "badge_url": f"https://github.com/{pipe['repo']}/actions/workflows/{run['path'].split('/')[-1]}/badge.svg",
            "run_url": run["html_url"],
            "commit_msg": (run.get("head_commit") or {}).get("message", "").split("\n")[0][:60],
            "date": run.get("created_at", "")[:10],
            "duration": duration,
        })
    return pipelines


# --- Main ---

if __name__ == "__main__":
    readme_path = root / "README.md"
    readme = readme_path.read_text()

    # Fetch all data sources
    print("Fetching active repos...")
    active_repos = fetch_active_repos(TOKEN)
    print(f"  Found {len(active_repos)} recently active repos")

    print("Fetching starred repos...")
    starred = fetch_starred(TOKEN)
    print(f"  Found {len(starred)} recent stars")

    print("Fetching blog posts...")
    posts = fetch_blog_posts()
    print(f"  Found {len(posts)} posts")

    print("Fetching pipeline status...")
    pipelines = fetch_pipeline_status(TOKEN)
    print(f"  Found {len(pipelines)} pipelines")

    print("Discovering npm packages...")
    package_names = discover_npm_packages()
    print(f"  Found {len(package_names)} @rmdes/* packages on npm")

    print("Fetching npm downloads...")
    npm_packages, npm_total = asyncio.run(fetch_npm_downloads(package_names))
    print(f"  {len(npm_packages)} packages, {npm_total:,} total monthly downloads")

    # Build markdown sections
    active_lines = []
    for r in active_repos[:8]:
        branch = r.get("defaultBranchRef") or {}
        commit = branch.get("target") or {}
        msg = commit.get("messageHeadline", "")[:60]
        date = commit.get("committedDate", "")[:10]
        desc = (r.get("description") or "")[:80]
        line = "[{}]({}) — {}".format(r["name"], r["url"], desc)
        if msg:
            line += "\n<br>`{}` ({})".format(msg, date)
        active_lines.append(line)
    active_md = "\n\n".join(active_lines)
    readme = replace_chunk(readme, "active_repos", active_md)

    starred_md = "\n\n".join(
        "[{}]({}) — {}".format(
            s["nameWithOwner"],
            s["url"],
            (s.get("description") or "")[:80],
        )
        for s in starred
    )
    readme = replace_chunk(readme, "starred", starred_md)

    posts_md = "\n\n".join(
        "[{title}]({url}) - {published}".format(**p)
        for p in posts
    )
    readme = replace_chunk(readme, "blog", posts_md)

    # Pipeline status section
    pipe_lines = []
    for p in pipelines:
        status_icon = "passing" if p["conclusion"] == "success" else "failing"
        pipe_lines.append(
            f"[![{p['label']}]({p['badge_url']})]({p['run_url']})\n"
            f"**{p['label']}** — {p['description']}\n"
            f"Last build: `{p['commit_msg']}` ({p['date']}"
            + (f", {p['duration']}" if p['duration'] else "")
            + ")"
        )
    pipeline_md = "\n\n".join(pipe_lines)
    readme = replace_chunk(readme, "pipelines", pipeline_md)

    # npm section: total + top 5 visible + collapsible rest
    def pkg_row(pkg):
        short_name = pkg["name"].replace("@rmdes/", "")
        return (
            f"| [{short_name}](https://www.npmjs.com/package/{pkg['name']}) "
            f"| {pkg['downloads']:,} |"
        )

    top_visible = 5
    npm_lines = [
        f"**{npm_total:,}** downloads last month across **{len(npm_packages)}** packages\n",
        "| Package | Downloads |",
        "|---------|-----------|",
    ]
    for pkg in npm_packages[:top_visible]:
        npm_lines.append(pkg_row(pkg))

    if len(npm_packages) > top_visible:
        rest = npm_packages[top_visible:]
        npm_lines.append("")
        npm_lines.append(f"<details><summary>See all {len(npm_packages)} packages</summary>")
        npm_lines.append("")
        npm_lines.append("| Package | Downloads |")
        npm_lines.append("|---------|-----------|")
        for pkg in rest:
            npm_lines.append(pkg_row(pkg))
        npm_lines.append("")
        npm_lines.append("</details>")

    npm_md = "\n".join(npm_lines)
    readme = replace_chunk(readme, "npm_stats", npm_md)

    readme_path.write_text(readme)
    print("README.md updated!")
