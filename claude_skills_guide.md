# The Complete Guide to Building Skills for Claude

## What is a Skill?

A skill is a set of instructions packaged as a simple folder that teaches Claude how to handle specific tasks.

**Key Benefits:**
- Generate frontend designs from specs consistently
- Conduct research with consistent methodology
- Create documents following your team style guide
- Orchestrate multi-step processes reliably

---

## Technical Structure

### Required Files

| File | Purpose |
|------|--------|
| SKILL.md | Main instruction file |
| frontmatter.yaml | Metadata |
| instructions.md | Workflow logic |
| templates/ | Optional templates |
| config/ | Optional config |

### Frontmatter Format

```yaml
name: my-skill
description: Brief description
version: "1.0.0"
author: Your Name
category: workflow
```

### Common Errors

| Error | Cause | Solution |
|-------|-------|--------|
| Could not find SKILL.md | File not named exactly SKILL.md | Rename to SKILL.md |
| Invalid frontmatter | YAML formatting issue | Check delimiters |

---

## Choosing Your Approach

### Problem-First vs Tool-First

| Approach | Description | Example |
|----------|-------------|---------|
| Problem-First | Users describe outcomes | "I need to set up a workspace" |
| Tool-First | Users have tools | "I have Notion MCP connected" |

---

## Core Patterns

### Pattern 1: Sequential Workflow
**Use when:** Multi-step processes in specific order

```markdown
-# Workflow: Onboard New Customer
--# Step 1: Create Account
Call MCP tool: create_customer
--# Step 2: Setup Payment
Call MCP tool: setup_payment_method
```

### Pattern 2: Multi-MCP Coordination
**Use when:** Workflows span multiple services

### Pattern 3: Conditional Logic
**Use when:** Decisions based on input or tool results

```markdown
-# Decision: Process Payment
IF compliance passed:
  - Call payment processing MCP tool
ELSE:
  - Flag for review
```

### Pattern 4: Storage Selection
**Use when:** Choosing where to store data

### Pattern 5: Domain-Specific Intelligence
**Use when:** Your skill adds specialized knowledge

---

## Advanced Patterns

### Pattern 6: Error Handling & Recovery

```markdown
-# Error Handling: Payment Failed
IF payment declined:
  - Check fraud score
  - If low: retry with different method
  - If high: flag for manual review
  - Log incident
```

### Pattern 7: Context Management

**Techniques:**
- Summarize long conversations
- Extract relevant snippets
- Maintain running context
- Reset when needed

### Pattern 8: Parallel Execution

```markdown
-# Parallel: Gather Information
--# Task 1: Search documentation
--# Task 2: Check API status
--# Task 3: Review recent tickets
THEN:
  - Synthesize findings
  - Present consolidated report
```

---

## Best Practices

### 1. Keep Skills Modular
- Each skill should do ONE thing well
- Reuse common patterns
- Document dependencies

### 2. Write Clear Instructions
- Use imperative voice
- Be specific about parameters
- Include examples
- Add edge cases

### 3. Test Thoroughly
- Test normal flows
- Test error conditions
- Test edge cases
- Document findings

### 4. Version Control
- Track changes
- Document breaking changes
- Maintain changelog

---

## Example: Complete Skill Structure

```
my-skill/
├── SKILL.md
├── frontmatter.yaml
├── instructions.md
├── templates/
│   ├── welcome-email.md
│   └── onboarding-checklist.md
└── config/
    └── settings.json
```

---

## Troubleshooting

### "Skill not loading"
- Check SKILL.md exists
- Verify frontmatter.yaml valid
- Ensure no syntax errors

### "Tool not found"
- Verify MCP tools available
- Check tool names match
- Review permissions

### "Workflow stuck"
- Check for infinite loops
- Add timeout handling
- Implement retry logic

---

## Resources

- [Claude Skills Documentation](https://docs.anthropic.com)
- [MCP Protocol Spec](https://modelcontextprotocol.io)
- [Skill Template Repository](https://github.com/anthropics/skills)

---

## Conclusion

Building skills for Claude is about:

1. **Understanding the structure** - SKILL.md, frontmatter, instructions
2. **Choosing the right pattern** - Sequential, conditional, parallel
3. **Writing clear instructions** - Specific, actionable, well-documented
4. **Testing thoroughly** - Normal flows, edge cases, error handling
5. **Iterating** - Refine based on feedback and results

Start simple, build complexity gradually, and always document your work.

**Happy skill-building! 🚀**
**Use when:** Your skill adds specialized knowledge

---

## Getting Started

1. Create a folder for your skill
2. Add SKILL.md with frontmatter
3. Document your workflow
4. Test with Claude
5. Iterate and improve

---

## Advanced Patterns

### Pattern 6: Error Handling & Recovery

```markdown
-# Error Handling: Payment Failed
IF payment declined:
  - Check fraud score
  - If low: retry with different method
  - If high: flag for manual review
  - Log incident
```

### Pattern 7: Context Management

**Techniques:**
- Summarize long conversations
- Extract relevant snippets
- Maintain running context
- Reset when needed

### Pattern 8: Parallel Execution

```markdown
-# Parallel: Gather Information
--# Task 1: Search documentation
--# Task 2: Check API status
--# Task 3: Review recent tickets
THEN:
  - Synthesize findings
  - Present consolidated report
```

---

## Best Practices

### 1. Keep Skills Modular
- Each skill should do ONE thing well
- Reuse common patterns
- Document dependencies

### 2. Write Clear Instructions
- Use imperative voice
- Be specific about parameters
- Include examples
- Add edge cases

### 3. Test Thoroughly
- Test normal flows
- Test error conditions
- Test edge cases
- Document findings

### 4. Version Control
- Track changes
- Document breaking changes
- Maintain changelog

---

## Example: Complete Skill Structure

```
my-skill/
├── SKILL.md
├── frontmatter.yaml
├── instructions.md
├── templates/
│   ├── welcome-email.md
│   └── onboarding-checklist.md
└── config/
    └── settings.json
```

---

## Troubleshooting

### "Skill not loading"
- Check SKILL.md exists
- Verify frontmatter.yaml valid
- Ensure no syntax errors

### "Tool not found"
- Verify MCP tools available
- Check tool names match
- Review permissions

### "Workflow stuck"
- Check for infinite loops
- Add timeout handling
- Implement retry logic

---

## Resources

- [Claude Skills Documentation](https://docs.anthropic.com)
- [MCP Protocol Spec](https://modelcontextprotocol.io)
- [Skill Template Repository](https://github.com/anthropics/skills)

---

## Conclusion

Building skills for Claude is about:

1. **Understanding the structure** - SKILL.md, frontmatter, instructions
2. **Choosing the right pattern** - Sequential, conditional, parallel
3. **Writing clear instructions** - Specific, actionable, well-documented
4. **Testing thoroughly** - Normal flows, edge cases, error handling
5. **Iterating** - Refine based on feedback and results

Start simple, build complexity gradually, and always document your work.

**Happy skill-building! 🚀**

