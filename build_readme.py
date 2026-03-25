import asyncio
import httpx
import json
import pathlib
import re
import os
import sys
from python_graphql_client import GraphqlClient

root = pathlib.Path(__file__).parent.resolve()
client = GraphqlClient(endpoint="https://api.github.com/graphql")

TOKEN = os.environ.get("RMDES_TOKEN", "")

RMDES_NPM_PACKAGES = [
    "@rmdes/indiekit-endpoint-activitypub",
    "@rmdes/indiekit-endpoint-comments",
    "@rmdes/indiekit-endpoint-conversations",
    "@rmdes/indiekit-endpoint-cv",
    "@rmdes/indiekit-endpoint-files",
    "@rmdes/indiekit-endpoint-funkwhale",
    "@rmdes/indiekit-endpoint-github",
    "@rmdes/indiekit-endpoint-homepage",
    "@rmdes/indiekit-endpoint-lastfm",
    "@rmdes/indiekit-endpoint-linkedin",
    "@rmdes/indiekit-endpoint-micropub",
    "@rmdes/indiekit-endpoint-microsub",
    "@rmdes/indiekit-endpoint-podroll",
    "@rmdes/indiekit-endpoint-posts",
    "@rmdes/indiekit-endpoint-rss",
    "@rmdes/indiekit-endpoint-share",
    "@rmdes/indiekit-endpoint-syndicate",
    "@rmdes/indiekit-endpoint-webmention-io",
    "@rmdes/indiekit-endpoint-webmention-sender",
    "@rmdes/indiekit-endpoint-youtube",
    "@rmdes/indiekit-frontend",
    "@rmdes/indiekit-post-type-page",
    "@rmdes/indiekit-preset-eleventy",
    "@rmdes/indiekit-syndicator-bluesky",
    "@rmdes/indiekit-syndicator-indienews",
    "@rmdes/indiekit-syndicator-linkedin",
    "@rmdes/indiekit-syndicator-mastodon",
]


def replace_chunk(content, marker, chunk, inline=False):
    r = re.compile(
        r"<!\-\- {} starts \-\->.*<!\-\- {} ends \-\->".format(marker, marker),
        re.DOTALL,
    )
    if not inline:
        chunk = "\n{}\n".format(chunk)
    chunk = "<!-- {} starts -->{}<!-- {} ends -->".format(marker, chunk, marker)
    return r.sub(chunk, content)


# --- GitHub Releases (GraphQL, paginated) ---

GRAPHQL_RELEASES_QUERY = """
query($after: String) {
  user(login: "rmdes") {
    repositories(first: 100, privacy: PUBLIC, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        name
        url
        releases(last: 1) {
          totalCount
          nodes {
            name
            tagName
            publishedAt
            url
          }
        }
      }
    }
  }
}
"""


def fetch_releases(oauth_token):
    releases = []
    has_next_page = True
    after = None

    while has_next_page:
        data = client.execute(
            query=GRAPHQL_RELEASES_QUERY,
            variables={"after": after},
            headers={"Authorization": "Bearer {}".format(oauth_token)},
        )
        repos = data["data"]["user"]["repositories"]
        for repo in repos["nodes"]:
            if not repo["releases"]["totalCount"]:
                continue
            release = repo["releases"]["nodes"][0]
            if not release["publishedAt"]:
                continue
            tag = release["name"] or release["tagName"] or ""
            # Strip repo name prefix from release name
            tag = tag.replace(repo["name"], "").strip()
            releases.append({
                "repo": repo["name"],
                "repo_url": repo["url"],
                "release": tag,
                "url": release["url"],
                "published_at": release["publishedAt"],
                "published_day": release["publishedAt"].split("T")[0],
            })
        page_info = repos["pageInfo"]
        has_next_page = page_info["hasNextPage"]
        after = page_info["endCursor"]

    releases.sort(key=lambda r: r["published_at"], reverse=True)
    return releases


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


async def fetch_npm_downloads():
    """Fetch monthly download counts for all @rmdes packages."""
    sem = asyncio.Semaphore(5)

    async def fetch_one(http, pkg):
        async with sem:
            return await http.get(
                f"https://api.npmjs.org/downloads/point/last-month/{pkg}"
            )

    async with httpx.AsyncClient(timeout=30) as http:
        tasks = [fetch_one(http, pkg) for pkg in RMDES_NPM_PACKAGES]
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


# --- Main ---

if __name__ == "__main__":
    readme_path = root / "README.md"
    readme = readme_path.read_text()

    # Fetch all data sources
    print("Fetching GitHub releases...")
    releases = fetch_releases(TOKEN)
    print(f"  Found {len(releases)} releases")

    print("Fetching starred repos...")
    starred = fetch_starred(TOKEN)
    print(f"  Found {len(starred)} recent stars")

    print("Fetching blog posts...")
    posts = fetch_blog_posts()
    print(f"  Found {len(posts)} posts")

    print("Fetching npm downloads...")
    npm_packages, npm_total = asyncio.run(fetch_npm_downloads())
    print(f"  {len(npm_packages)} packages, {npm_total:,} total monthly downloads")

    # Build markdown sections
    releases_md = "\n\n".join(
        "[{repo} {release}]({url}) - {published_day}".format(**r)
        for r in releases[:8]
    )
    readme = replace_chunk(readme, "recent_releases", releases_md)

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

    # npm section: total + top packages table
    top_pkgs = npm_packages[:10]
    npm_lines = [
        f"**{npm_total:,}** downloads last month across **{len(npm_packages)}** packages\n",
        "| Package | Downloads |",
        "|---------|-----------|",
    ]
    for pkg in top_pkgs:
        short_name = pkg["name"].replace("@rmdes/", "")
        npm_lines.append(
            f"| [{short_name}](https://www.npmjs.com/package/{pkg['name']}) "
            f"| {pkg['downloads']:,} |"
        )
    npm_md = "\n".join(npm_lines)
    readme = replace_chunk(readme, "npm_stats", npm_md)

    readme_path.write_text(readme)
    print("README.md updated!")
