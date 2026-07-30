# Plan: Compare weather + rank Berlin/Tokyo/Seattle for an outdoor weekend

## Goal
Compare **current weather** and the **3-day forecast** for **Berlin, Tokyo, and Seattle**, then propose a ranking for the best **outdoor weekend**.

## Todo steps
1. **Fetch current weather** for Berlin, Tokyo, Seattle.
2. **Fetch 3-day forecasts** (days=3) for Berlin, Tokyo, Seattle.
3. **Extract outdoor-relevant signals** from both current + forecast:
   - temperature (current + weekend span within 3 days)
   - precipitation / rain risk
   - wind (if available)
   - cloudiness/visibility indicators (if available)
4. **Compare and rank** cities using a simple outdoor-focused rubric (prefer: lower precipitation risk, comfortable/mild temps, lower wind).
5. **Present results** in a side-by-side comparison plus final ranking and brief recommendation.

## Exploratory check
- If city names might be strict, call **list_cities** to confirm accepted entries.

## Output format
- Side-by-side table or bullet comparison per city: current + forecast highlights.
- Final ranked list (1–3) with one-paragraph justification.
