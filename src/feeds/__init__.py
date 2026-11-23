"""RSS feeds from Hacker News and from other RSS or Atom feeds, whose items carry each article's image and excerpt."""

# The feeds Worker serves this repo's GitHub Pages site on its own hostname
# (mbologna/infra, terraform/dns-michelebologna.net/feeds.tf), so subscriptions
# outlive the hosting.
PAGE_URL = "https://feeds.michelebologna.net/"
STATE_NAME = "state.json"
USER_AGENT = "Mozilla/5.0 (compatible; feeds; +https://github.com/mbologna/feeds)"
