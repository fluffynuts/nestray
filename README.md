Nestray
---

A very simply systray application for Thunderbird, with the following features:

1. can run without Thunderbird already running - will launch it if required
2. relies on Thunderbird to do mail sync - no email credentials required
3. will update it's icon when there's new mail & notify of new mail
4. will remind you every 1/2 hour if there is unread mail still in your inbox
5. all configured via ~/.config/nestray.ini

NOTE: currently only supports IMAP mailboxes (since that's what I use, and
I wanted something to replace birdtray that just worked for me - feel free
to fork & PR for pop3 support).

### Requirements
- pyqt6, or pyqt5 (fails over to pyqt5)
- kdotool
  - the best option is to install with your package manager,
    but if it's not available, there is a bundled binary you
    can enable via config. I built this binary myself, from
    the kdotool github repository. If you don't trust me, that's
    also ok:
    - clone https://github.com/jinliu/kdotool
    - build with `cargo build --release`
    - either copy the output (target/release/kdotool) to
      somewhere in your path, or overwrite the kdotool binary
      in this repository and enable using the bundled kdotool,
      though this may give you issues when attempting to update
      this repository locally.


### Configuration

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
# use the kdotool binary bundled alongside nestray.py (1 = enabled, 0 = disabled)
UseBundledKdoTool=1
# enable debug logging (1 = enabled, 0 = disabled)
Debug=0
```
