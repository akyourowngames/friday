# Tool Routing Categories

This is the first routing gate before exact tool selection. KING embeds these
category examples, picks one category, then compares the user request only
against tools listed in that category's `tools:` line. This keeps unrelated
tools out of the decision instead of dropping every request into the full
registry.

Format:

```text
### category_name
- tools: tool_a, tool_b
- example user phrasing
```

Use broad intent families, not narrow phrase tables. Add or move tools here
when a request family is going to the wrong part of the system.

## Categories

### external_retrieval
- tools: web_search, web_fetch, reddit, hackernews, weather, datetime_info, browser_read_page, browser_extract, navigator
- fetch latest reddit threads
- show me current posts from reddit
- show me reddit threads about a topic
- find reddit discussion threads on something
- get live news or web results
- read a web page at a url or extract details from a website
- check weather, time, distance, or route information
- what is the weather in a city
- what time is it in a city

### local_file_intelligence
- tools: folder_watcher, file_read, file_write, file_list, note_save, note_read, note_update, note_delete, note_list, note_search, gallery
- read a local file like routing_policy.md or config.txt
- open and show the contents of a file on disk
- inspect the watched folder
- count indexed files or file types
- find files in local folders
- read, write, list, update, or search local files and notes
- read a local file by its name or path like a .md or .txt file
- show saved generated images

### telegram_delivery
- tools: telegram_watcher
- send a file through telegram
- check telegram watcher status
- find allowed-zone files for telegram delivery
- turn telegram file watch notifications on or off
- use the telegram courier service

### personal_memory
- tools: memory_recall, memory_remember, memory_forget, memory_assess, memory_extract, life_timeline, proactive_check
- remember this fact
- recall what you know about me
- forget a stored memory
- assess memory health or sync the Obsidian memory vault
- extract memories from Obsidian user files
- summarize recent life context

### media_generation
- tools: youtube_play, playlist, imagine, camera_vision, screenshot
- play music or manage playlist
- generate an image
- inspect camera or screenshot
- what is this, what am i looking at, describe what you see
- look through the camera and tell me what this is
- show visual media

### local_device_control
- tools: system_control, keyboard_press, keyboard_shortcut, terminal, process_control, clipboard, system_pulse, calc
- control volume brightness media keys or keyboard shortcuts
- run a local command or open an app
- inspect or stop processes
- read or write clipboard
- check system vitals or calculate a value

### scheduling
- tools: reminder, reminder_fire, scheduler_schedule, scheduler_list, scheduler_cancel, scheduler_run_due, daily_maintenance
- set a reminder
- schedule something for later
- list or cancel queued tasks
- run due scheduled work
- run maintenance

### project_management
- tools: project_track, project_status, project_focus, project_detail, project_alerts, project_decisions, project_archive, project_resurrect, project_brief_schedule, project_export
- track a new project or give an update on one
- king track this and keep an eye on it
- track this: launch something by a deadline
- track this project: build a thing by end of month
- i am starting a project to build something, track it for me
- update on my project: I finished a task and another is blocked
- i finished the task, here is my progress update
- i just completed a piece of the work
- the work is blocked on something, log that blocker
- we are cutting or dropping a feature from the project
- i decided to change direction on the project
- tell the manager I finished or got blocked on something
- ask how my projects are doing or what to focus on
- king status across all my projects
- deep dive on a single project's status and health
- ask what we decided about something or replay decisions
- archive a finished project or revive a cold one
- turn on a daily morning project briefing
- export my projects to obsidian or get a context brief for another assistant

### connected_integrations
- tools: composio, browser_login_session
- check calendar email drive tasks slack notion or github through connected accounts
- what's on my calendar tomorrow
- check my latest emails
- find my recent google drive files
- search my notion for meeting notes
- send a slack message
- start a browser login session
- use an external account integration

### tool_admin
- tools: find_tools, load_tool, tool_manifest_audit, tool_verification_pipeline
- find a tool capability
- load a tool for this turn
- audit tool manifest and registry alignment
- run tool verification checks
