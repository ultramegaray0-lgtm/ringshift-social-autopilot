import requests
from pathlib import Path

from env_check import require_env

BUFFER_API_URL = "https://api.buffer.com"
BUFFER_ORGANIZATION_ID = "6ac7b3d929f568e502ba8638"
BUFFER_CHANNEL_ID = "6ac7b4286a5c39ccb6523f7c"

BASE_DIR = Path(__file__).resolve().parent.parent
ANALYTICS_FILE = BASE_DIR / "data" / "analytics.md"


PAGE_SIZE = 50
MAX_PAGES = 20          # hard stop: 1000 posts, so a bad cursor can never loop forever
METRICS_FILE = BASE_DIR / "data" / "metrics.json"

POSTS_QUERY = """
query GetPosts($organizationId: OrganizationId!, $first: Int, $after: String) {
  posts(
    first: $first
    after: $after
    input: {
      organizationId: $organizationId
    }
  ) {
    edges {
      node {
        id
        text
        dueAt
        sentAt
        status
        metrics {
          name
          description
          value
          unit
        }
        metricsUpdatedAt
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

# The original single-page query, kept as a fallback in case the paginated form is rejected.
SINGLE_PAGE_QUERY = POSTS_QUERY.replace(", $first: Int, $after: String", "").replace(
    "    first: $first\n    after: $after\n", "").replace(
    "    pageInfo {\n      hasNextPage\n      endCursor\n    }\n", "")


def _request(api_key, query, variables):
    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json={"query": query, "variables": variables},
        timeout=30
    )

    if response.status_code != 200:
        print(response.text)
        raise RuntimeError(f"Buffer API request failed (HTTP {response.status_code}).")

    return response.json()


def get_posts():
    """Every post Buffer returns, following cursors (Buffer's default page is only ~10 posts).

    Pagination follows https://developers.buffer.com/guides/pagination.md (first/after on
    `posts`, pageInfo.hasNextPage/endCursor). If Buffer rejects the paginated query on the
    first page, falls back to the single-page query so the daily run keeps working."""
    # Fail fast with a clear message instead of sending "Bearer " and getting a 401.
    api_key = require_env("BUFFER_API_KEY")["BUFFER_API_KEY"]

    edges = []
    cursor = None
    seen_cursors = set()

    for page in range(1, MAX_PAGES + 1):
        variables = {"organizationId": BUFFER_ORGANIZATION_ID, "first": PAGE_SIZE}
        if cursor:
            variables["after"] = cursor

        data = _request(api_key, POSTS_QUERY, variables)

        if data.get("errors"):
            if page == 1:
                print("Paginated query rejected; falling back to a single page:", data["errors"])
                single = _request(api_key, SINGLE_PAGE_QUERY, {"organizationId": BUFFER_ORGANIZATION_ID})
                if single.get("errors"):
                    print(single["errors"])
                    raise RuntimeError(single["errors"])
                return single["data"]["posts"]["edges"]

            print(data["errors"])
            raise RuntimeError(data["errors"])

        posts = data["data"]["posts"]
        edges.extend(posts.get("edges") or [])

        info = posts.get("pageInfo") or {}
        cursor = info.get("endCursor")

        if not info.get("hasNextPage") or not cursor or cursor in seen_cursors:
            break

        seen_cursors.add(cursor)
    else:
        print(f"Stopped after {MAX_PAGES} pages; older posts were not fetched.")

    # A post can appear on two pages if one was queued while paging.
    unique, seen = [], set()
    for edge in edges:
        post_id = (edge.get("node") or {}).get("id")
        if post_id is not None and post_id in seen:
            continue
        seen.add(post_id)
        unique.append(edge)

    return unique


def metrics_record(post):
    """One post as plain data for data/metrics.json. Only what Buffer returned; nothing derived."""
    values = {}
    for metric in post.get("metrics") or []:
        if metric.get("name") is not None and metric.get("value") is not None:
            values[metric["name"]] = metric["value"]

    return {
        "id": post.get("id"),
        "status": post.get("status"),
        "dueAt": post.get("dueAt"),
        "sentAt": post.get("sentAt"),
        "text": post.get("text", ""),
        "metrics": values,
        "metricsUpdatedAt": post.get("metricsUpdatedAt"),
    }


def save_metrics(posts, path=None):
    import json

    path = path or METRICS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [metrics_record(item["node"]) for item in posts]
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return records


def save_analytics(posts):
    ANALYTICS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with ANALYTICS_FILE.open("w", encoding="utf-8") as file:
        file.write("# RINGSHIFT — X ANALYTICS\n\n")
        file.write(f"Posts returned by Buffer: {len(posts)}\n\n")

        for item in posts:
            post = item["node"]

            file.write("## Post\n\n")
            file.write(f"ID: {post.get('id')}\n\n")
            file.write(f"Status: {post.get('status')}\n\n")
            file.write(f"Scheduled: {post.get('dueAt')}\n\n")
            file.write(f"Published: {post.get('sentAt')}\n\n")

            file.write("### Post\n\n")
            file.write(post.get("text", "") + "\n\n")

            file.write("### Metrics\n\n")

            metrics = post.get("metrics") or []

            if not metrics:
                file.write("No metrics available yet.\n\n")
            else:
                for metric in metrics:
                    name = metric.get("name")
                    value = metric.get("value")
                    unit = metric.get("unit")

                    file.write(
                        f"- {name}: {value} ({unit})\n"
                    )

                file.write(
                    f"\nMetrics updated: {post.get('metricsUpdatedAt')}\n\n"
                )

            file.write("---\n\n")


def main():
    print("Fetching Ringshift posts and metrics from Buffer...")

    posts = get_posts()

    print(f"Found {len(posts)} posts.")

    save_analytics(posts)
    save_metrics(posts)

    print("Saved analytics to:")
    print(ANALYTICS_FILE)
    print(METRICS_FILE)


if __name__ == "__main__":
    main()