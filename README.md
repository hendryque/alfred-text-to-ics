# Text to ICS

Alfred workflow that pulls events out of a block of text and hands them to
Calendar.

Copy an email, a ticket confirmation, a message full of dates. Type `cal`.
The text goes to OpenAI, comes back as structured events, and Calendar opens
with them ready to add.

![icon](icon.png)

## Requirements

* Alfred 5 with the Powerpack
* Python 3.9 or newer (the one from the Command Line Tools works)
* An OpenAI API key

Standard library only. Nothing to install.

## Setup

**Get an API key** from [platform.openai.com](https://platform.openai.com/api-keys).
The workflow bills against your own account. A short text costs a fraction of a
cent with the default model.

**Install the workflow.** Download `Alfred-Text-to-ICS.alfredworkflow` from
[releases](https://github.com/hendryque/alfred-text-to-ics/releases) and
double-click it.

**Add the key.** Open Alfred, Workflows, Text to ICS, Configure workflow. Or
leave the field empty and put the key in `~/.config/openai-key` instead.

The key is marked non-exporting, so it stays behind if you share the workflow.

## Usage

```
cal
```

with text on the clipboard, or

```
cal Dentist on 15 October at 9:30, Dr Berger, Vienna
```

Events more than 30 days in the past are dropped, on the assumption they are
stale context rather than something you want in the calendar. A multi-day event
counts as past only once its last day has gone by, so a running trip survives.

A continuous span becomes one multi-day entry: a holiday flat booked from the
12th to the 14th is a single all-day banner. When the text instead gives hours
that repeat on each day, you get one entry per day, so a fair open 10 to 18
leaves the evenings in between free.

Each event gets two reminders, one day before and one hour before.

## Configuration

All three are optional and live in the workflow configuration, or as
environment variables if you run the script directly.

| Setting | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | reads `~/.config/openai-key` | |
| `TEXT_TO_ICS_MODEL` | `gpt-4.1` | Any chat model that supports structured output |
| `TEXT_TO_ICS_TZ` | the Mac's own zone | IANA name, for example `America/New_York` |

The time zone matters. Events are written with an explicit `VTIMEZONE` block
built from that zone's real offsets, so daylight saving is handled wherever you
are, including the southern hemisphere.

`TEXT_TO_ICS_TZ` is only the fallback. When a location pins the zone by itself,
that event uses it instead: a Bangkok hotel check-in at 15:00 stays 15:00 in
Bangkok rather than becoming 15:00 at home. Each zone in play gets its own
`VTIMEZONE` block, and a zone the model invents is ignored in favour of the
fallback.

## Command line

`src/text_to_ics.py` runs on its own:

```sh
OPENAI_API_KEY=sk-... ./src/text_to_ics.py "Standup every Monday 9am"
pbpaste | OPENAI_API_KEY=sk-... ./src/text_to_ics.py
TEXT_TO_ICS_TZ=Asia/Tokyo OPENAI_API_KEY=sk-... ./src/text_to_ics.py "Flight 3 Nov 14:20"
```

## What gets sent

The text you pass goes to OpenAI's API. Nothing else leaves your machine, and
nothing is stored by this workflow. Check OpenAI's data policy for what happens
on their side, and think twice before running it over anything confidential.

## Troubleshooting

Failures show up as a notification saying what went wrong; the full response
body stays in Alfred's debug console.

**No API key.** Neither the workflow field nor `~/.config/openai-key` had one.

**OpenAI rejected the key (401).** Wrong or revoked key.

**Rate limited or out of quota (429).** Your account has no credit, or you hit
a limit.

**No events found in text.** The model saw nothing datelike. Adding an explicit
year often helps.

**Skipping event with bad date.** One event came back malformed; the rest still
go through. The message names which one.

**Nothing happens at all.** A failure normally shows as a notification, so
silence points at the workflow rather than at OpenAI: check that
`text_to_ics.py` is executable.

## Releasing

`publish.py` builds the bundle and creates the GitHub release:

```sh
./publish.py --check     # run the guards, build nothing
./publish.py 1.2.3       # must match version in workflow/info.plist
```

It refuses a dirty tree, requires `main` to match `origin/main`, and checks that
`src/` and `workflow/` are byte-identical, that the plist parses, that the script
compiles under the Command Line Tools Python, and that no configuration value is
baked into the bundle. The zip is built from `HEAD` rather than the working
directory, so the asset always matches the tag.

## Licence

MIT, see [LICENSE](LICENSE).

The icon comes from Apple's SF Symbols (`calendar.badge.plus`). Apple allows SF
Symbols in software running on Apple platforms and restricts redistribution of
the artwork by itself. As a workflow icon that is covered. Replace it if you
port this elsewhere.
