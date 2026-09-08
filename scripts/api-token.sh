#!/usr/bin/env bash
# Print the bearer token for the /event API.
# Paste it into the macOS/iOS Shortcut, or into ~/.config/calendar-connect/env.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require gcloud
require terraform

secret api-token
echo
