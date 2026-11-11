# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Delegated. The user chose plain static HTML, CSS and vanilla JavaScript in one file, with no framework, deployable as a Claude artifact. Chosen for the easiest and most reliable deploy on Claude.

## Users

People who plan trips with an AI assistant. They ask their assistant for flights and hotels and do not want to open search sites themselves. Install is the step they take once they decide to try it. Developers who already run MCP clients are a secondary reader, not the page's audience.

## Product Purpose

viajante runs flight and hotel searches on the user's own computer and returns the rows, the links and the status of each search to the assistant. Success means the visitor installs viajante into the assistant they already use.

## Positioning

viajante does not book. It returns what the public travel pages show, with the link to each result and the status of every search, and the assistant decides what to do with it. A booking product could not truthfully say that, and it does not promise a cheapest option. It refuses to guess: empty results are reported as their real reason, not as "none found".

## Operating Context

The visitor reads the landing page on a desktop or phone, picks an assistant, copies that assistant's setup message, and pastes it into that assistant. Claude and ChatGPT ask Web or Desktop app before copying. Web keeps the browser limits: claude.ai needs a public connector URL, and ChatGPT on the web uses a Secure MCP Tunnel. Desktop adds a local server, Claude Desktop through claude_desktop_config.json and the ChatGPT desktop app through its MCP servers setting. Muse Code and Hermes Agent install a local server. Grok Bot starts the server on its own computer. Instinct runs in a Python sandbox. Every message ends with the same fallback: write a Python file, import viajante, and run it, using get_flights for a route spec and search_hotels with HotelQuery plus a currency.

## Capabilities and Constraints

- Runs locally. No server, no account, no API keys.
- Search results: prices, itinerary details, source text and links where the page provides them.
- Interfaces: a command line (`viajante`), a Python library, and an MCP server over stdio.
- Requires Python 3.10 or later. The npx route also needs `uv`.
- Claude and ChatGPT open Web and Desktop app, then that pill copies its setup message. The other pills copy immediately. The visible label stays "Install in <name>".
- Assistants that cannot start a program on the visitor's machine are told so in that message. The page does not offer a hosted viajante.
- Undecided: whether a hosted version will ever exist. Not planned. The page must not imply one.

## Brand Commitments

- Name: viajante, lowercase.
- Logo: a route mark (two points joined by an arc) with the lowercase wordmark. Supplied as `logo/viajante-logo.svg` in the working scratch folder; the mark is drawn in pastel blue with ink and blue dots.
- Assistant marks are drawn from the official logos the user supplied or from Simple Icons (Anthropic, OpenAI, Meta). Grok Bot and Instinct marks are redrawn from the user's screenshots.
- Voice: short, plain, friendly, English. No hype.
- Look: white, pastel, Apple-like minimal. Less cartoony than the earlier draft. This is a binding visual constraint from the user.

## Evidence on Hand

- Repository README: install commands, MCP configuration, Python 3.10 requirement, no API keys, runs on the user's machine.
- MCP guidance: status words `provider_empty`, `filtered_out` and `not_loaded`, and the rule that only `provider_empty` means none found. Used on the page as-is.
- No customer, testimonial, pricing or benchmark evidence exists. Future work must not invent any.
- Search provider names are kept off the landing page by the user's choice. Do not name them.

## Product Principles

- Show the evidence, not an opinion.
- Say what it does and what it does not do. Do not describe how it fails.
- Keep the page to the install decision. Nothing that does not help the visitor pick an assistant.
- Do not name the sources the searches read from.

## Accessibility & Inclusion

Not established by the user. Default to the standard: real buttons, visible focus, text contrast that meets 4.5:1, and no information carried by color alone.
