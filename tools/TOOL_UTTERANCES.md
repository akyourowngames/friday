# Tool Utterances

This is the markdown control surface for utterance-based tool routing.

## Why utterances

Tool descriptions are developer artifacts that say what a tool *is*. User
queries say what a user *wants*. Comparing those two vocabularies via embedding
similarity creates collisions when descriptions overlap.

The fix is to compare a query against example queries (utterances) per tool —
like to like, user language to user language. The router embeds every utterance
listed here as its own row, then scores each tool by the highest similarity of
any of its utterances against the query. A tool wins not because its
description sounds like the query, but because somebody once phrased the same
thing this user just said.

To fix a routing collision, edit the utterances for the affected tool. No code
change, no embedding rebuild of the rest of the catalog: only the changed
section is re-embedded on next run. This is semantic routing, not a keyword
table or phrase-match shortcut: utterances are matched by embedding similarity,
never by literal substring.

## Format

Each section header is a registered tool name (`## tool_name`). Bullet points
under it are example user phrasings. A tool with no section here falls back to
its registry description and examples, so missing tools degrade gracefully
while migration is in progress.

Keep utterances short, in the user's voice, and varied. Cover real phrasings
(commands, questions, fragments) rather than restating the tool's purpose.

## web_search
- search the web for {topic}
- find news about {topic}
- look up the latest {topic}
- what is happening with {topic}
- google {topic}
- find articles on {topic}
- search for {topic} online

## web_fetch
- open this url and read it for me
- fetch the content of {url}
- read this page {url}
- summarize what is on this page
- what does this article say
- pull the text from this link

## file_read
- read this file
- show me the contents of {path}
- open {path} and show what is inside
- print the file at {path}
- what does {path} contain

## file_write
- save this to a file
- write this content to {path}
- create a file at {path} with this text
- append this to {path}
- update {path} with the following

## file_list
- list files in {path}
- show me what is in this folder
- show directory entries in {path}
- ls {path}
- what files are inside {path}
- list the contents of the directory
- show raw file listing for {path}
- dir {path}

## terminal
- open notepad
- launch chrome
- run python {script}
- start the file explorer
- open task manager
- run npm install
- execute this shell command
- start vscode
- open calculator
- run this script for me

## system_control
- volume up
- turn the volume down
- mute the audio
- unmute
- make the screen brighter
- dim the brightness
- pause the music
- play the song
- skip this track

## keyboard_press
- press control alt delete
- send the win plus l shortcut
- hit the enter key
- press tab three times

## keyboard_shortcut
- run the new tab shortcut
- trigger the screenshot shortcut
- fire the lock screen shortcut by name

## imagine
- generate an image of {description}
- draw a picture of {description}
- create art showing {description}
- make me a render of {description}
- imagine {description}
- paint me {description}
- generate a photo-style image of {description}

## gallery
- show my saved images
- open the image gallery
- list the pictures i generated
- delete the last image i made
- find an image i generated about {topic}

## note_save
- save this as a note
- create a note titled {title}
- write down this thought as a note
- store this in my notes

## note_read
- show me the note about {topic}
- open my note titled {title}
- read back the {title} note

## note_update
- update my note about {topic}
- add this to the {title} note
- change the {title} note to say {content}

## note_delete
- delete the note titled {title}
- remove my notes about {topic}

## note_list
- list my notes
- what notes do i have
- show notes tagged {tag}

## note_search
- search my notes for {query}
- find notes mentioning {query}

## memory_recall
- what do you remember about me
- what have i told you about {topic}
- recall what i said about {topic}
- pull up what you know about {topic}

## memory_remember
- remember that {fact}
- save this fact about me: {fact}
- store {fact} in long-term memory

## memory_forget
- forget what i said about {topic}
- remove memories about {topic}

## memory_assess
- check memory health
- audit the memory index
- run a memory integrity check

## weather
- what is the weather in {city}
- weather forecast for {city}
- is it going to rain in {city}
- temperature in {city} right now

## calc
- calculate {expression}
- what is {math expression}
- compute {expression}
- evaluate {expression}

## clipboard
- read my clipboard
- what is on my clipboard
- copy {text} to clipboard
- put this on my clipboard

## screenshot
- take a screenshot
- capture my screen
- screenshot my desktop

## system_pulse
- how is my pc doing
- check cpu and memory usage
- report system vitals
- top processes using memory
- battery status
- disk usage

## process_control
- find processes named {name}
- kill the {name} process
- terminate {name}
- is {name} running

## reminder
- remind me to {task} in {time}
- set a reminder for {time}
- remind me at {time} to {task}

## scheduler_schedule
- schedule {action} for later
- run {action} at {time}
- queue {action} in {time}

## scheduler_list
- list scheduled items
- what is queued in the scheduler
- show pending scheduled actions

## scheduler_cancel
- cancel scheduled item {id}
- stop the scheduled {action}

## scheduler_run_due
- run anything that is due
- fire due scheduler items

## daily_maintenance
- run the daily maintenance routine
- trigger maintenance now
- do today's maintenance pass

## proactive_check
- do you have anything to tell me
- anything proactive for me
- what should i know

## life_timeline
- summarize what was going on for me recently
- give me a timeline of recent life events
- recall my recent life as a story

## find_tools
- can you find a tool that {capability}
- search the tool catalog for {capability}
- is there a tool for {capability}

## load_tool
- load the {name} tool
- enable the {name} tool for this turn

## youtube_play
- play {song} on youtube
- search youtube for {query}
- play music by {artist}
- find the {song} video

## playlist
- show my youtube playlist
- queue {song} into my playlist
- list saved tracks

## hackernews
- show top hacker news stories
- what is on hacker news
- search hacker news for {query}
- show comments for hn story {id}

## reddit
- show me reddit front page
- top posts on r/{subreddit}
- search reddit for {query}
- get reddit comments on {post}

## navigator
- distance from {a} to {b}
- how far is {a} to {b}
- driving time from {a} to {b}
- route between {a} and {b}

## datetime_info
- what time is it
- current date and time
- what time is it in {city}
- today's date

## camera_vision
- what is in front of the camera
- describe what you see
- look through the camera and tell me
- analyze this image

## browser_read_page
- open the page {url} and read it
- load {url} in the browser

## browser_extract
- extract fields from {url}
- scrape {url} for {fields}

## browser_login_session
- log into {site} for me
- start a browser login session for {site}

## folder_watcher
- what files are in the watched folder
- how many python files are indexed
- find files about {topic} in the index
- show the latest file added to the index
- show indexed file stats
- inspect indexed file id {id}
- search the indexed folder for {query}
- what types of files are in the watched folder
- summarize the indexed folder contents

## tool_manifest_audit
- audit the tool manifest
- check manifest and registry alignment

## tool_verification_pipeline
- run the verification pipeline
- execute the markdown verification checks

## composio
- check my calendar [slug:GOOGLECALENDAR_EVENTS_LIST]
- what's on my calendar today or tomorrow [slug:GOOGLECALENDAR_EVENTS_LIST]
- show my upcoming calendar events [slug:GOOGLECALENDAR_EVENTS_LIST]
- check my schedule for the week [slug:GOOGLECALENDAR_EVENTS_LIST]
- list my upcoming meetings [slug:GOOGLECALENDAR_EVENTS_LIST]
- read my latest emails [slug:GMAIL_FETCH_EMAILS]
- check my latest emails [slug:GMAIL_FETCH_EMAILS]
- search my email for a message [slug:GMAIL_FETCH_EMAILS]
- search my gmail [slug:GMAIL_FETCH_EMAILS]
- find a file in my google drive [slug:GOOGLEDRIVE_FIND_FILE]
- find my recent google drive files [slug:GOOGLEDRIVE_FIND_FILE]
- list my google tasks [slug:GOOGLETASKS_LIST_ALL_TASKS]
- send a slack message to {channel} [slug:SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL]
- create a notion page about {topic} [slug:NOTION_CREATE_NOTION_PAGE]
- search my notion for meeting notes [slug:NOTION_SEARCH_NOTION_PAGE]
- list my github issues [slug:GITHUB_LIST_REPOSITORY_ISSUES]
