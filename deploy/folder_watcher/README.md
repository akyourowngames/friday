# KING Folder Watcher Deployment Templates

These templates are examples for running the folder watcher outside a foreground
terminal. Update paths and user names before installing them.

## Foreground

```powershell
python folder_watcher_service.py run --config tools/FOLDER_WATCHER_CONFIG.md
```

## Linux systemd

Copy `folder-watcher.service` to `/etc/systemd/system/folder-watcher.service`,
edit the repository path and user, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now folder-watcher
```

## macOS launchd

Copy `com.king.folder-watcher.plist` to `~/Library/LaunchAgents/`, edit paths,
then run:

```bash
launchctl load ~/Library/LaunchAgents/com.king.folder-watcher.plist
```

## Docker

Build from the repository root:

```bash
docker build -f deploy/folder_watcher/Dockerfile -t king-folder-watcher .
docker compose -f deploy/folder_watcher/docker-compose.yml up
```

## Remote HTTPS

Use `nginx-folder-watcher.conf` as the reverse proxy template when the service
runs behind HTTPS. Replace the example domain and certificate paths, keep the
upstream pointed at the configured folder watcher API port, and set an
`auth_token` in `tools/FOLDER_WATCHER_CONFIG.md` before exposing the endpoint.

```bash
sudo cp deploy/folder_watcher/nginx-folder-watcher.conf /etc/nginx/sites-available/folder-watcher
sudo ln -s /etc/nginx/sites-available/folder-watcher /etc/nginx/sites-enabled/folder-watcher
sudo nginx -t
sudo systemctl reload nginx
```
