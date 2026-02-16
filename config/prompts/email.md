# Email & Proton Mail Reference

## Account
- **Address**: wendys-mobile-oracle@proton.me
- **Credentials**: Stored in secrets.py (`proton_email`, `proton_password`, `proton_recovery`)
- **Backups**: wendys_folder/, channels/coding/, ~/.config/wendy/, and Pi (/workbench/wendy-credentials/)
- **Personal accounts file**: ~/.config/wendy/credentials (ProtonMail and GitHub login for monsterwendy account)

## Email CLI Client
- **Script**: `/data/wendy/channels/coding/proton-mail/email_client.py`
- Reads inbox, sends emails, replies. Uses hydroxide bridge (Go) for IMAP/SMTP.
- **Start bridge first**: `bash /data/wendy/channels/coding/proton-mail/start_hydroxide.sh` (IMAP:1143, SMTP:1025)
- Bridge password stored in secrets.py as `hydroxide_bridge_pass`
- **Commands**: `inbox [count]`, `read <num>`, `send <to> <subj> <body>`, `reply <num> <body>`, `check`
- Hydroxide binary: `/root/go/bin/hydroxide` (requires Go at `/usr/local/go/bin/go`)
- Bridge must be running before email_client.py works. Not auto-started (no cron in container) - start manually each session.

## Email & RSS Notification Service
- Runs on Pi as systemd service `wendy-email.service`. Posts to Discord via webhook ("Wendy's Inbox").
- **Script**: `/data/wendy/channels/coding/proton-mail/email_checker.py` (deployed to Pi at `/workbench/wendy-email/email_checker.py`)
- **Deploy updates**: SFTP via paramiko to `/workbench/wendy-email/email_checker.py` then `sudo systemctl restart wendy-email`
- Webhook: posts new emails and RSS feed updates to coding channel
- RSS feeds configured in `RSS_FEEDS` list in email_checker.py (currently: NASA APOD)
- **CRITICAL: When "Wendy's Inbox" posts a NASA APOD update with an embedded image, ALWAYS download and look at the image yourself.** Delta wants you to appreciate and comment on the space photos. Download the image URL from the embed, view it with Read + analyze_file, and share a brief thought about it in chat.
- To add new RSS feeds: edit `RSS_FEEDS` in email_checker.py, redeploy to Pi, restart service

## MIME Encoding Bug (FIXED 2026-02-16)
- Russian sender names were showing as raw `=?utf-8?q?...?=` encoded-words
- Fix: Applied `decode_subject()` to sender names in both `startup_announcement()` and `check_for_new()`
- Deployed to Pi via SFTP (paramiko)
