# Custom Attributes Guide

For the full conceptual guide and use case explanations, see the companion blog post:

**[Unlock Smarter Ad Optimization: How to Send Custom Attributes via Amazon Ads Conversion API](https://advertising.amazon.com/blog/custom-attributes-capi)**

## Quick Reference

### Attribute Limits
- Maximum 13 custom attributes per event
- 10 fully custom + 3 reserved (`brand`, `product`, `category`)

### Payload Structure

```json
"customData": [
  { "name": "attribute_name", "dataType": "STRING", "value": "your_value" }
]
```

### Naming Rules
- Use `snake_case` consistently
- No leading/trailing spaces
- No special characters
- Keep names descriptive but concise

### Data Type Encoding
All values are sent as strings regardless of their semantic type:

| Semantic Type | dataType Field | Value Format |
|---------------|---------------|--------------|
| Text | `STRING` | `"premium"` |
| Number | `STRING` | `"49.99"` |
| Boolean | `STRING` | `"true"` / `"false"` |
| Integer | `STRING` | `"12"` |

### Reserved Attributes

These three attribute names have special meaning in Amazon Ads reporting:

| Name | Purpose |
|------|---------|
| `brand` | Product or company brand name |
| `product` | Product identifier or name |
| `category` | Product or service category |

## Running the Examples

See the [main README](../README.md) for setup and execution instructions.
