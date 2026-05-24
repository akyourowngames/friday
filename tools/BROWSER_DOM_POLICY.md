# Browser DOM Read Policy

Markdown control surface for `browser_read_page` and DOM modes on `browser_extract`.
Not a router or keyword table.

## Limits

- max_blocks: 80
- max_block_chars: 600
- max_links: 40
- max_headings: 30

## Main Content Selectors

Use `||` between selectors. First match wins.

- main_selectors: main || article || [role=main] || #content || .content || body

## Skip Tags

- skip_tags: script, style, noscript, svg, path, iframe

## Heading Tags

- heading_tags: h1, h2, h3, h4, h5, h6

## Block Tags

- block_tags: p, li, td, th, blockquote, pre, figcaption, dd, dt
