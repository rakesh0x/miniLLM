Q1. what did you build?

i built an llm gateway that handles api keys, token-based billing, and credit limits. users get their own api key, and the gateway charges them based on actual token usage. i also added response caching and output guardrails, with a simple chat ui to test everything.

Q2. how does a request work?

the request goes from the browser through a next.js proxy to the fastapi backend. the backend validates the api key, checks the cache and available credits, reserves the estimated cost, and then starts streaming from groq. once the request finishes, it charges the actual usage and releases the unused credits.

Q3. what were your most important decisions?

database: i started with sqlite, but concurrency tests exposed locking and connection-pooling issues. i moved to postgres because credit updates need to work reliably across concurrent requests.

billing: charging a flat amount per request felt wrong because responses use different numbers of tokens. i went with a reserve-and-settle approach: reserve the estimated maximum upfront, then charge actual usage after generation.

concurrency: i use a conditional database update to reserve credits only when enough balance is available. this avoids two concurrent requests spending the same credits.

Q4.why enforce budgets at the gateway instead of trusting callers?

because even honest callers can make mistakes under concurrency. two requests might check the same balance and both assume the credits are available. the gateway handles the check and reservation atomically in postgres, so only requests with enough available credits can proceed.

Q5.what happens when two requests hit a nearly exhausted key?

i have a test that sends 40 concurrent requests against a key with 25 credits and checks that the balance never goes negative. postgres handles the competing updates, and requests that can't reserve enough credits are rejected.

one issue i've found is that some synchronous database calls still block the async event loop. a synthetic test showed roughly a 6× performance penalty, so fixing those calls is a priority.

Q6.how does model fallback work?

if a model fails before producing its first token, the gateway tries the next model. once streaming starts, it doesn't switch models because combining two responses could produce a messy answer. all three models currently use the same provider, so this isn't multi-provider failover.

Q7.what did you deliberately leave out?

i left out semantic caching. embeddings would add latency to requests, and similar questions don't always have the same answer. for now, exact-match caching is simpler and safer. i'd revisit it once i have real cache-hit data.

Q8.which decision are you least confident about?

the cache normalisation. it handles differences in capitalisation and whitespace, but not rephrased questions. i'd like to measure how often that causes misses before making the matching more aggressive.

Q9.where does it break, and what would you fix first?

first, i'd fix the blocking database calls and test the backend under load. second, i'd secure the key-management endpoints, which currently assume a trusted operator despite the backend being publicly accessible. i'd also add alembic so database schema changes are properly versioned.