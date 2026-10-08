import os
import requests
from pathlib import Path

BUFFER_API_URL = "https://api.buffer.com"
BUFFER_ORGANIZATION_ID = "6ac7b3d929f568e502ba8638"
BUFFER_CHANNEL_ID = "6ac7b4286a5c39ccb6523f7c"

BASE_DIR = Path(__file__).resolve().parent.parent
ANALYTICS_FILE = BASE_DIR / "data" / "analytics.md"


def get_posts():
    query = """
    query GetPosts($organizationId: OrganizationId!) {
      posts(
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
      }
    }
    """

    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Authorization": f"Bearer {os.environ['BUFFER_API_KEY']}",
            "Content-Type": "application/json"
        },
        json={
            "query": query,
            "variables": {
                "organizationId": BUFFER_ORGANIZATION_ID
            }
        },
        timeout=30
    )

    if response.status_code != 200:
        print(response.text)
        raise RuntimeError("Buffer API request failed.")

    data = response.json()

    if data.get("errors"):
        print(data["errors"])
        raise RuntimeError(data["errors"])

    return data["data"]["posts"]["edges"]


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

    print(f"Saved analytics to:")
    print(ANALYTICS_FILE)


if __name__ == "__main__":
    main()