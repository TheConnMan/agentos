---
name: weather
description: Look up a location's weather forecast using a live web search. Invoke whenever the user asks about the weather, whether to expect rain, temperature, or what to wear or plan for outdoor activities.
allowed-tools:
  - WebSearch
  - WebFetch
---

# Weather

## When to run
The user asks about the weather, a forecast, temperature, rain or snow chances, or whether a day suits an outdoor plan.

## How to answer
1. Determine the location and day. If the user named them, use them. If not, ask one short question instead of guessing.
2. Run a web search for the forecast, e.g. `<city> weather forecast <day>`. Prefer a national weather service or a major forecast provider.
3. Report, in two or three sentences: expected high and low, sky conditions, and precipitation chance. Include the location and day so there is no ambiguity.
4. Name the source of the forecast at the end.

## Hard rules

- Never invent a forecast. If the search returns nothing usable, say so and name what you tried.
- Temperatures in Fahrenheit first, Celsius in parentheses.
- Keep the reply short enough to read in Slack without expanding.
