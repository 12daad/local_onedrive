# ADR-001: CFAPI full hydration

Status: accepted

Use `CF_HYDRATION_POLICY_FULL`, not `ALWAYS_FULL`. Microsoft documents that FULL
allows dehydrated placeholders but requires complete content before the opening
I/O completes. ALWAYS_FULL rejects operations that create a dehydrated
placeholder. Python downloads a complete cache file, then C# submits aligned
chunks through `CfExecute`.

Progressive/random-range hydration is outside v0.1.

