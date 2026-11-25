# Outreach sheet · {{date}}

Offer: {{settings.offer}}
Leads: {{count leads}}

## To contact

{{#each leads status=new}}
### {{@number}}. {{name}} · {{platform}} · fit {{fit_score}}/5
- Profile: {{profile_url}}
{{#if headline}}- About: {{headline}}
{{/if}}{{#if fit_reason}}- Why: {{fit_reason}}
{{/if}}{{#if email}}- Email: {{email}}{{#if contact_source}} (source: {{contact_source}}){{/if}}
{{/if}}{{#if phone}}- Phone: {{phone}}
{{/if}}
{{#if first_message}}**Message to send:**

```text
{{first_message}}
```
{{else}}_No message drafted yet: run "Draft first messages"._
{{/if}}
- [ ] Sent

{{/each}}

## Replied: send the call link

{{#each leads status=replied}}
### {{name}} · {{platform}}
- Profile: {{profile_url}}
{{#if email}}- Email: {{email}}
{{/if}}
{{#if follow_up}}**Follow-up to send:**

```text
{{follow_up}}
```
{{else}}_No follow-up drafted yet: run "Draft follow-ups"._
{{/if}}
- [ ] Sent

{{/each}}

## Waiting for a reply

{{#each leads status=contacted}}- {{name}} ({{platform}}): {{profile_url}}
{{/each}}

## Calls booked

{{#each leads status=booked,call_link_sent}}- {{name}} ({{status}}): {{profile_url}}
{{/each}}
