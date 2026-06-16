# Review Examples

Sanitized from real review output, these show the comment shape Bubo aims
for: concrete impact, visible evidence, a local fix, and confidence.

## Resource Leak

```text
Issue: S3 object stream is opened before the no-op branch and can leak.
Impact: A scheduled no-op path returns after opening the stream. Repeated runs
can leak HTTP connections and exhaust the adapter pool.
Evidence: The stream is opened before checking whether the file already exists;
the return branch never copies or closes it.
Fix: Open the stream only in the download branch, or wrap it in
try-with-resources so every path closes it.
Confidence: 0.91
```

## API Compatibility

```text
Issue: offset is now a required request parameter.
Impact: Existing clients that omitted offset under the previous endpoint
contract now fail before request handling.
Evidence: The previous handler accepted a missing primitive query parameter as
0; the new handler marks offset required without a default.
Fix: Add a default value or make the parameter optional to preserve the previous
contract, unless the breaking change is intentional and documented.
Confidence: 0.95
```

## Validation Response Loss

```text
Issue: Validation violations with the same message collapse to one response row.
Impact: A request with multiple invalid fields can hide some failed fields from
the client.
Evidence: A sorted set compares violations only by message. Comparator equality
removes distinct violations that share the same message.
Fix: Sort without using the set for de-duplication, or compare by property path
plus message and invalid value.
Confidence: 0.94
```

## Ask When Context Is Missing

```text
Question: The new in-memory lock appears process-local.
Evidence: The lock is stored in process memory, but the deployment shape is not
shown.
Question: Can this service run on more than one instance? If yes, this needs
database-level serialization or a cross-instance lock.
Confidence: 0.78
```
