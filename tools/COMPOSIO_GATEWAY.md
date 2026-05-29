# Composio Gateway

This file is the markdown control surface for KING's Composio bridge. It keeps external app capabilities limited, auditable, and easy to expand without exposing every Composio toolkit directly to the model.

## Runtime

- enabled: true
- base_url: https://backend.composio.dev/api/v3.1
- api_key_env: COMPOSIO_API_KEY
- user_id_env: KING_COMPOSIO_USER_ID
- session_id_env: KING_COMPOSIO_SESSION_ID
- default_timeout_ms: 20000
- max_response_chars: 12000
- semantic_slug_resolution: true
- semantic_slug_min_score: 0.35
- semantic_slug_min_margin: 0.03
- lexical_slug_resolution: true
- lexical_slug_min_score: 0.45
- lexical_slug_min_margin: 0.02
- create_sessions_with_search: true
- create_sessions_with_manage_connections: true
- create_sessions_with_workbench: false

## Enabled Toolkits

- github
- gmail
- googlecalendar
- googledocs
- googledrive
- googlesheets
- googletasks
- notion
- slack
- googlemeet
- googleslides
## Enabled Tools

- GITHUB_GET_A_REPOSITORY | toolkit: github | risk: read | enabled: true | note: get repository details and metadata after GitHub is connected
- GITHUB_LIST_REPOSITORY_ISSUES | toolkit: github | risk: read | enabled: true | note: list repository issues after GitHub is connected
- GITHUB_LIST_STARGAZERS | toolkit: github | risk: read | enabled: true | note: list repository stargazers after GitHub is connected

- GMAIL_ADD_LABEL_TO_EMAIL | toolkit: gmail | risk: write | enabled: true | note: modify labels on Gmail messages with confirmation
- GMAIL_CREATE_EMAIL_DRAFT | toolkit: gmail | risk: write | enabled: true | note: create Gmail email drafts with confirmation
- GMAIL_CREATE_LABEL | toolkit: gmail | risk: write | enabled: true | note: create Gmail labels with confirmation
- GMAIL_FETCH_EMAILS | toolkit: gmail | risk: read | enabled: true | note: fetch and search Gmail emails after Gmail is connected
- GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID | toolkit: gmail | risk: read | enabled: true | note: read a specific Gmail message by message id
- GMAIL_GET_CONTACTS | toolkit: gmail | risk: read | enabled: true | note: read Google contacts through Gmail
- GMAIL_GET_PROFILE | toolkit: gmail | risk: read | enabled: true | note: read Gmail profile after Gmail is connected
- GMAIL_LIST_DRAFTS | toolkit: gmail | risk: read | enabled: true | note: list Gmail drafts
- GMAIL_LIST_LABELS | toolkit: gmail | risk: read | enabled: true | note: list Gmail labels
- GMAIL_LIST_SEND_AS | toolkit: gmail | risk: read | enabled: true | note: list Gmail send-as aliases
- GMAIL_REPLY_TO_THREAD | toolkit: gmail | risk: write | enabled: true | note: reply to Gmail threads with explicit confirmation
- GMAIL_SEND_EMAIL | toolkit: gmail | risk: write | enabled: true | note: send Gmail email with explicit confirmation
- GOOGLECALENDAR_CREATE_EVENT | toolkit: googlecalendar | risk: write | enabled: true | note: create Google Calendar events with confirmation
- GOOGLECALENDAR_EVENTS_GET | toolkit: googlecalendar | risk: read | enabled: true | note: get one Google Calendar event by id
- GOOGLECALENDAR_EVENTS_LIST | toolkit: googlecalendar | risk: read | enabled: true | note: list Google Calendar events
- GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS | toolkit: googlecalendar | risk: read | enabled: true | note: list events across Google calendars
- GOOGLECALENDAR_FIND_EVENT | toolkit: googlecalendar | risk: read | enabled: true | note: find Google Calendar events
- GOOGLECALENDAR_FIND_FREE_SLOTS | toolkit: googlecalendar | risk: read | enabled: true | note: find free slots in Google Calendar
- GOOGLECALENDAR_PATCH_EVENT | toolkit: googlecalendar | risk: write | enabled: true | note: update Google Calendar events with confirmation
- GOOGLECALENDAR_QUICK_ADD | toolkit: googlecalendar | risk: write | enabled: true | note: quick add Google Calendar events with confirmation
- GOOGLECALENDAR_SETTINGS_LIST | toolkit: googlecalendar | risk: read | enabled: true | note: list Google Calendar settings
- GOOGLEDOCS_CREATE_DOCUMENT | toolkit: googledocs | risk: write | enabled: true | note: create Google Docs documents with confirmation
- GOOGLEDOCS_CREATE_DOCUMENT_MARKDOWN | toolkit: googledocs | risk: write | enabled: true | note: create Google Docs from markdown with confirmation
- GOOGLEDOCS_EXPORT_DOCUMENT_AS_PDF | toolkit: googledocs | risk: read | enabled: true | note: export Google Docs documents as PDF
- GOOGLEDOCS_GET_DOCUMENT_BY_ID | toolkit: googledocs | risk: read | enabled: true | note: get Google Docs document metadata by id
- GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT | toolkit: googledocs | risk: read | enabled: true | note: read Google Docs content as plain text
- GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN | toolkit: googledocs | risk: write | enabled: true | note: replace Google Docs content from markdown with confirmation
- GOOGLEDOCS_UPDATE_DOCUMENT_SECTION_MARKDOWN | toolkit: googledocs | risk: write | enabled: true | note: update Google Docs sections from markdown with confirmation
- GOOGLEDRIVE_CREATE_COMMENT | toolkit: googledrive | risk: write | enabled: true | note: create Drive file comments with confirmation
- GOOGLEDRIVE_CREATE_FILE | toolkit: googledrive | risk: write | enabled: true | note: create Google Drive files with confirmation
- GOOGLEDRIVE_CREATE_FILE_FROM_TEXT | toolkit: googledrive | risk: write | enabled: true | note: create Google Drive text files with confirmation
- GOOGLEDRIVE_CREATE_FOLDER | toolkit: googledrive | risk: write | enabled: true | note: create Google Drive folders with confirmation
- GOOGLEDRIVE_DOWNLOAD_FILE | toolkit: googledrive | risk: read | enabled: true | note: download a Google Drive file by id
- GOOGLEDRIVE_FIND_FILE | toolkit: googledrive | risk: read | enabled: true | note: find files and folders in Google Drive
- GOOGLEDRIVE_FIND_FOLDER | toolkit: googledrive | risk: read | enabled: true | note: find folders in Google Drive
- GOOGLEDRIVE_GET_ABOUT | toolkit: googledrive | risk: read | enabled: true | note: read Google Drive profile and storage info
- GOOGLEDRIVE_LIST_CHILDREN_V2 | toolkit: googledrive | risk: read | enabled: true | note: list children in a Drive folder
- GOOGLEDRIVE_LIST_COMMENTS | toolkit: googledrive | risk: read | enabled: true | note: list comments for a Drive file
- GOOGLEDRIVE_LIST_PERMISSIONS | toolkit: googledrive | risk: read | enabled: true | note: list Drive file permissions
- GOOGLEDRIVE_LIST_SHARED_DRIVES | toolkit: googledrive | risk: read | enabled: true | note: list shared drives
- GOOGLESHEETS_BATCH_GET | toolkit: googlesheets | risk: read | enabled: true | note: batch read Google Sheets ranges
- GOOGLESHEETS_CREATE_GOOGLE_SHEET1 | toolkit: googlesheets | risk: write | enabled: true | note: create Google Sheets spreadsheets with confirmation
- GOOGLESHEETS_GET_SHEET_NAMES | toolkit: googlesheets | risk: read | enabled: true | note: list sheet names in a spreadsheet
- GOOGLESHEETS_QUERY_TABLE | toolkit: googlesheets | risk: read | enabled: true | note: query Google Sheets tables
- GOOGLESHEETS_SHEET_FROM_JSON | toolkit: googlesheets | risk: write | enabled: true | note: create Google Sheets from JSON with confirmation
- GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND | toolkit: googlesheets | risk: write | enabled: true | note: append values to Google Sheets with confirmation
- GOOGLESHEETS_UPDATE_VALUES_BATCH | toolkit: googlesheets | risk: write | enabled: true | note: batch update Google Sheets values with confirmation
- GOOGLESHEETS_VALUES_GET | toolkit: googlesheets | risk: read | enabled: true | note: read Google Sheets cell values
- GOOGLESHEETS_VALUES_UPDATE | toolkit: googlesheets | risk: write | enabled: true | note: update Google Sheets values with confirmation
- GOOGLETASKS_CREATE_TASK_LIST | toolkit: googletasks | risk: write | enabled: true | note: create Google Tasks lists with confirmation
- GOOGLETASKS_GET_TASK | toolkit: googletasks | risk: read | enabled: true | note: get a Google Task by id
- GOOGLETASKS_INSERT_TASK | toolkit: googletasks | risk: write | enabled: true | note: create Google Tasks tasks with confirmation
- GOOGLETASKS_LIST_ALL_TASKS | toolkit: googletasks | risk: read | enabled: true | note: list all Google Tasks across lists
- GOOGLETASKS_LIST_TASKS | toolkit: googletasks | risk: read | enabled: true | note: list tasks from one Google Tasks list
- GOOGLETASKS_LIST_TASK_LISTS | toolkit: googletasks | risk: read | enabled: true | note: list Google Tasks task lists
- GOOGLETASKS_PATCH_TASK | toolkit: googletasks | risk: write | enabled: true | note: update Google Tasks tasks with confirmation
- NOTION_APPEND_TABLE_BLOCKS | toolkit: notion | risk: write | enabled: true | note: append Notion table blocks with confirmation
- NOTION_APPEND_TASK_BLOCKS | toolkit: notion | risk: write | enabled: true | note: append Notion task blocks with confirmation
- NOTION_APPEND_TEXT_BLOCKS | toolkit: notion | risk: write | enabled: true | note: append Notion text blocks with confirmation
- NOTION_CREATE_DATABASE | toolkit: notion | risk: write | enabled: true | note: create Notion databases with confirmation
- NOTION_CREATE_NOTION_PAGE | toolkit: notion | risk: write | enabled: true | note: create Notion pages with confirmation
- NOTION_FETCH_ALL_BLOCK_CONTENTS | toolkit: notion | risk: read | enabled: true | note: read Notion block content recursively
- NOTION_FETCH_BLOCK_CONTENTS | toolkit: notion | risk: read | enabled: true | note: read Notion block children
- NOTION_FETCH_DATA | toolkit: notion | risk: read | enabled: true | note: fetch Notion workspace pages or databases
- NOTION_FETCH_DATABASE | toolkit: notion | risk: read | enabled: true | note: read Notion database schema
- NOTION_INSERT_ROW_DATABASE | toolkit: notion | risk: write | enabled: true | note: insert rows into Notion databases with confirmation
- NOTION_QUERY_DATABASE | toolkit: notion | risk: read | enabled: true | note: query Notion databases
- NOTION_QUERY_DATABASE_WITH_FILTER | toolkit: notion | risk: read | enabled: true | note: query Notion databases with filters
- NOTION_SEARCH_NOTION_PAGE | toolkit: notion | risk: read | enabled: true | note: search Notion pages and databases
- SLACK_ADD_REACTION_TO_AN_ITEM | toolkit: slack | risk: write | enabled: true | note: add Slack reactions with confirmation
- SLACK_ASSISTANT_SEARCH_CONTEXT | toolkit: slack | risk: read | enabled: true | note: search Slack messages files channels and users
- SLACK_CHAT_POST_MESSAGE | toolkit: slack | risk: write | enabled: true | note: send Slack messages with confirmation
- SLACK_FETCH_CONVERSATION_HISTORY | toolkit: slack | risk: read | enabled: true | note: read Slack conversation history
- SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION | toolkit: slack | risk: read | enabled: true | note: read Slack message threads
- SLACK_FIND_CHANNELS | toolkit: slack | risk: read | enabled: true | note: find Slack channels
- SLACK_LIST_PINNED_ITEMS | toolkit: slack | risk: read | enabled: true | note: list pinned Slack items
- SLACK_LIST_SCHEDULED_MESSAGES | toolkit: slack | risk: read | enabled: true | note: list scheduled Slack messages
- SLACK_OPEN_DM | toolkit: slack | risk: write | enabled: true | note: open Slack DMs with confirmation

- GITHUB_COMPARE_TWO_COMMITS | toolkit: github | risk: read | enabled: true | note: compare GitHub commits branches or tags
- GITHUB_CREATE_AN_ISSUE_COMMENT | toolkit: github | risk: write | enabled: true | note: comment on GitHub issues or pull requests with confirmation
- GITHUB_CREATE_A_PULL_REQUEST | toolkit: github | risk: write | enabled: true | note: create GitHub pull requests with confirmation
- GITHUB_DOWNLOAD_WORKFLOW_RUN_LOGS | toolkit: github | risk: read | enabled: true | note: download GitHub Actions workflow run logs
- GITHUB_FIND_PULL_REQUESTS | toolkit: github | risk: read | enabled: true | note: find GitHub pull requests
- GITHUB_GET_A_REPOSITORY_README | toolkit: github | risk: read | enabled: true | note: read a GitHub repository README
- GITHUB_GET_A_WORKFLOW_RUN | toolkit: github | risk: read | enabled: true | note: get a GitHub Actions workflow run
- GITHUB_GET_RAW_REPOSITORY_CONTENT | toolkit: github | risk: read | enabled: true | note: read raw GitHub repository file content
- GITHUB_LIST_PULL_REQUESTS | toolkit: github | risk: read | enabled: true | note: list GitHub repository pull requests
- GITHUB_LIST_PULL_REQUESTS_FILES | toolkit: github | risk: read | enabled: true | note: list files changed in a GitHub pull request
- GITHUB_LIST_REVIEWS_FOR_A_PULL_REQUEST | toolkit: github | risk: read | enabled: true | note: list GitHub pull request reviews
- GITHUB_LIST_REVIEW_COMMENTS_ON_A_PULL_REQUEST | toolkit: github | risk: read | enabled: true | note: list GitHub pull request review comments
- GOOGLEMEET_CREATE_MEET | toolkit: googlemeet | risk: write | enabled: true | note: create Google Meet spaces with confirmation
- GOOGLEMEET_GET_MEET | toolkit: googlemeet | risk: read | enabled: true | note: get Google Meet space details
- GOOGLEMEET_GET_RECORDINGS_BY_CONFERENCE_RECORD_ID | toolkit: googlemeet | risk: read | enabled: true | note: get Google Meet recordings by conference record
- GOOGLEMEET_GET_TRANSCRIPTS_BY_CONFERENCE_RECORD_ID | toolkit: googlemeet | risk: read | enabled: true | note: get Google Meet transcripts by conference record
- GOOGLEMEET_LIST_CONFERENCE_RECORDS | toolkit: googlemeet | risk: read | enabled: true | note: list Google Meet conference records
- GOOGLEMEET_LIST_PARTICIPANTS | toolkit: googlemeet | risk: read | enabled: true | note: list Google Meet participants
- GOOGLEMEET_LIST_RECORDINGS | toolkit: googlemeet | risk: read | enabled: true | note: list Google Meet recordings
- GOOGLEMEET_LIST_TRANSCRIPT_ENTRIES | toolkit: googlemeet | risk: read | enabled: true | note: list Google Meet transcript entries
- GOOGLEMEET_UPDATE_SPACE | toolkit: googlemeet | risk: write | enabled: true | note: update Google Meet spaces with confirmation
- GOOGLESLIDES_CREATE_PRESENTATION | toolkit: googleslides | risk: write | enabled: true | note: create Google Slides presentations with confirmation
- GOOGLESLIDES_CREATE_SLIDES_MARKDOWN | toolkit: googleslides | risk: write | enabled: true | note: create Google Slides from markdown with confirmation
- GOOGLESLIDES_GET_PAGE_THUMBNAIL2 | toolkit: googleslides | risk: read | enabled: true | note: get Google Slides page thumbnails
- GOOGLESLIDES_PRESENTATIONS_BATCH_UPDATE | toolkit: googleslides | risk: write | enabled: true | note: update Google Slides presentations with confirmation
- GOOGLESLIDES_PRESENTATIONS_COPY_FROM_TEMPLATE | toolkit: googleslides | risk: write | enabled: true | note: copy Google Slides from templates with confirmation
- GOOGLESLIDES_PRESENTATIONS_GET | toolkit: googleslides | risk: read | enabled: true | note: get Google Slides presentation details
- GOOGLESLIDES_PRESENTATIONS_PAGES_GET | toolkit: googleslides | risk: read | enabled: true | note: get Google Slides page details
## Argument Defaults

- GITHUB_GET_A_REPOSITORY | owner: local.owner | repo: local.repo
- GITHUB_LIST_REPOSITORY_ISSUES | owner: local.owner | repo: local.repo
- GITHUB_LIST_STARGAZERS | owner: local.owner | repo: local.repo
- GITHUB_CREATE_AN_ISSUE_COMMENT | owner: local.owner | repo: local.repo
- GITHUB_CREATE_A_PULL_REQUEST | owner: local.owner | repo: local.repo
- GITHUB_LIST_PULL_REQUESTS | owner: local.owner | repo: local.repo
- GITHUB_LIST_PULL_REQUESTS_FILES | owner: local.owner | repo: local.repo
- GITHUB_LIST_REVIEWS_FOR_A_PULL_REQUEST | owner: local.owner | repo: local.repo
- GITHUB_LIST_REVIEW_COMMENTS_ON_A_PULL_REQUEST | owner: local.owner | repo: local.repo
- GITHUB_COMPARE_TWO_COMMITS | owner: local.owner | repo: local.repo
- GITHUB_GET_RAW_REPOSITORY_CONTENT | owner: local.owner | repo: local.repo
- GITHUB_GET_A_REPOSITORY_README | owner: local.owner | repo: local.repo
- GITHUB_GET_A_WORKFLOW_RUN | owner: local.owner | repo: local.repo
- GITHUB_DOWNLOAD_WORKFLOW_RUN_LOGS | owner: local.owner | repo: local.repo

## Argument Default Placeholders

- values: owner, repo, repository

## Risk Policy

- read: allow after the tool slug is listed under Enabled Tools.
- write: require confirm=true in the tool call.
- destructive: require confirm=true and keep disabled until explicitly added to Enabled Tools.
- auth: only generate Composio hosted auth links for toolkits listed under Enabled Toolkits.

## Usage Contract

- KING exposes one registry tool named `composio`; it must not expose the whole Composio catalog as direct callable schemas.
- Session creation must use only the markdown-enabled toolkits and enabled tool slugs.
- Direct execution must reject any tool slug that is not listed as enabled here.
- Bulk tool installation may add exact Composio slugs to this markdown file through the gateway, but it must not enable fuzzy catalog results without explicit slugs.
- Auth connection should use the Composio tool-router session link flow for enabled toolkits, with optional alias and callback URL passed through provider fields rather than hardcoded app logic.
- Auth status should be read from the Composio session toolkit metadata; KING must not assume a toolkit is connected just because a link was created.
- Semantic slug resolution may map an imprecise requested slug to an already-enabled tool only when the configured score and margin gates pass.
- Lexical slug recovery may use tokens from enabled markdown policy entries and prefer lower-risk tied candidates; it must not use hardcoded phrase tables.
- Provider errors, missing API keys, missing sessions, and auth requirements must return structured errors instead of false-negative capability claims.
- Argument defaults are resolved by the gateway from local repo evidence such as `git remote.origin.url`; they fill missing values and markdown-listed placeholder values without overriding concrete user-supplied arguments.
- Never store Composio API keys, OAuth tokens, connected account secrets, or private provider responses in this markdown file.

## Local Test Prompts

- `composio status`
- `create a composio session for github`
- `connect github through composio`
- `check composio auth status`
- `list composio github tools for issues`
- `install composio tools GITHUB_LIST_REPOSITORY_ISSUES and GITHUB_LIST_STARGAZERS for github`
- `use composio to get repo details for owner akyourowngames repo friday`
- `use composio to get repo details for this repo`
- `use composio to list issues for owner akyourowngames repo friday`
- `connect gmail through composio`
- `connect googlecalendar through composio`
- `connect googledrive through composio`
- `connect googledocs through composio`
- `connect googlesheets through composio`
- `connect googlecontacts through composio`
- `connect googlemeet through composio`
- `connect googleslides through composio`
- `connect googletasks through composio`
- `connect slack through composio`
- `connect notion through composio`
- `get latest gmail emails through composio`
- `fetch my latest Gmail emails through composio`
- `find free slots in my Google Calendar tomorrow through composio`
- `find my recent Google Drive files through composio`
- `search my Google Contacts through composio`
- `create a Google Meet through composio`
- `create Google Slides from markdown through composio`
- `search Slack for project updates through composio`
- `search Notion pages for meeting notes through composio`
