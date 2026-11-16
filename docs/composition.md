# Composing with other MCP servers

Viajante returns provider evidence and does one job. Anything beyond that is the agent calling
another MCP server with fields viajante returned. These recipes name which fields to hand over
and which rule keeps the result honest. They name no tool of any other server: read its tool
list and use what it exposes.

## Calendar

Source: `validate_itinerary`, or the segments of the offers you validated.

1. Validate the legs in travel order. Build events only from a result whose `feasible` is `true`.
2. One event per segment from its own fields: departure date and clock, `arrival_date`,
   `departure_timezone` and `arrival_timezone`, flight number, origin and destination.
3. A segment missing a date or a zone has an unknown time. Leave it out or ask the traveller;
   never fill it from the airport.
4. Put the offer's `google_flights_url` in the event description. A query URL reproduces a
   query, not current availability.

A calendar entry is not a booking and not connection protection. `recheck_offer` the finalist
before the traveller buys.

## Spreadsheet or notes

Source: the rows of `search_flights`, `search_dates` or `search_hotels`.

- Write one row per offer with the fields as returned: amount and currency together, stops,
  duration, clocks, carrier, `status`, `completeness`.
- One column per currency. Do not add amounts in different currencies, and do not sum one-way
  fares into a round trip. A round trip's price is the provider's package total.
- Keep the envelope's `observed_at` on every row. A price in a sheet is a past observation.
- A hotel `price` is the total stay. Keep `selection_id` out of the sheet: it is valid only in
  the process that returned it.

Rows for `not_loaded` or `filtered_out` are not "no flights". Record them with their status, or
leave them out.

## Currency conversion

Viajante never converts. When the traveller wants another currency:

1. Keep the quoted amount and its currency. They are the evidence.
2. Ask a currency or exchange-rate MCP server for the rate. Record its name, the rate and its
   timestamp.
3. Show the converted amount as the caller's own, next to the quoted one: `EUR 412 (about
   USD 448 at 1.087, source and time named)`.

`verify_answer` flags amounts and currencies no search in this process returned, so a converted
amount is flagged by design. Treat that flag as the reminder to name the rate and its source.
