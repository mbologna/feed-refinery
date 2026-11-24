"""RSS feeds from Hacker News and from other RSS or Atom feeds, whose items carry each article's image and excerpt."""

# Where the feeds are served: the feeds Worker (mbologna/infra,
# terraform/dns-michelebologna.net/feeds.tf) serves them from Workers KV on its
# own hostname, so subscriptions outlive the hosting.
PAGE_URL = "https://feeds.michelebologna.net/"
STATE_NAME = "state.json"
HEALTH_NAME = "health.json"
USER_AGENT = "Mozilla/5.0 (compatible; feed-refinery; +https://github.com/mbologna/feed-refinery)"
