---
name: email-followup
description: "Automatically notify recipients after sending emails. Use when sending emails to important contacts."
category: communication
version: 1.0.0
examples:
  - "Send an email to Rohit about the meeting"
  - "Email the team about the project update"
---

# Email Followup Skill

## Purpose
After successfully sending an email, automatically notify the recipient and update them that the email was sent.

## When to Use
- After sending any email via `send_email` tool
- When emailing important contacts (rohit, team leads, clients)
- For any communication that requires confirmation

## Workflow

### 1. Send the Email
Use the `send_email` tool as normal:
```
send_email(to="rohit@example.com", subject="Meeting Update", body="...")
```

### 2. After Successful Send
Immediately after the email is sent successfully:

1. **Update Memory** - Store that you sent this email:
```
store_memory(
    fact_text="Sent email to [recipient] about [subject] on [date]",
    category="action",
    source="conversation",
    links={"person": ["recipient_name"], "project": ["project_name"]}
)
```

2. **Notify Recipient** - If they're on Telegram/messaging:
```
telegram_send_message(
    chat_id="recipient_chat_id",
    message="Hi, I just sent you an email about [subject]. Please check your inbox."
)
```

3. **Update Task Status** - If this was part of a task:
```
update_task(
    task_id="task_id",
    status="completed",
    notes="Email sent to [recipient]"
)
```

## Example Flow

**User:** "Send an email to Rohit about the meeting tomorrow"

**Agent actions:**
1. `send_email(to="rohit@company.com", subject="Meeting Tomorrow", body="Hi Rohit, ...")`
2. After success: `store_memory(fact_text="Sent email to Rohit about meeting on 2026-07-16", links={"person": ["Rohit"]})`
3. After success: `telegram_send_message(chat_id="rohit_telegram", message="Hi Rohit, I just sent you an email about tomorrow's meeting. Please check your inbox.")`

## Important Notes
- Only notify after confirming email was sent successfully
- Store in memory for future reference ("do you remember when I emailed Rohit?")
- Update relevant tasks/goals if applicable
- Don't spam - one notification per email is sufficient
