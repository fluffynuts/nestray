Nestray
---

A very simply systray application for Thunderbird, with the following features:

1. can run without Thunderbird already running - will launch it if required
2. relies on Thunderbird to do mail sync - no email credentials required
3. will update it's icon when there's new mail & notify of new mail
4. will remind you every 1/2 hour if there is unread mail still in your inbox
5. all configured via ~/.config/nestray.ini


~/.config/nestray.ini will be created for you on first run, and if any new
options are added, on an update, it will add the option with the default value

```
[General]
# the period between polls of the IMAP folder, in seconds
PollInterval=10
# the maximum time to poll for the thunderbird window when attempting to raise it, in seconds
RaiseTimeout=5
# whether to raise desktop notifications for unread mail (1 = enabled, 0 = disabled)
DesktopNotifications=1
# minutes to wait before re-notifying about the same unread count
RemindInterval=30
# enable debug logging (1 = enabled, 0 = disabled)
Debug=0
```
