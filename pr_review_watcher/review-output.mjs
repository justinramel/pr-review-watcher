import {readFileSync, writeFileSync} from 'node:fs';

// One narrowly scoped output tool; no shell, file reads, MCP, or delegation tools.
export default function (pi) {
  const schema = JSON.parse(readFileSync(process.env.REVIEW_SCHEMA_PATH, 'utf8'));
  pi.registerTool({
    name: 'structured_output', label: 'Review result',
    description: 'Submit your complete review result as the final action. Do not emit prose afterward.',
    parameters: schema,
    async execute(_id, result) {
      writeFileSync(process.env.REVIEW_OUTPUT_PATH, JSON.stringify(result, null, 2), {mode: 0o600});
      return {content: [{type: 'text', text: 'Review result saved.'}], details: result, terminate: true};
    },
  });
  const prior = JSON.parse(readFileSync(process.env.REVIEW_PRIOR_PATH, 'utf8'));
  if (prior.length) {
    pi.registerTool({
      name: 'submit_feedback', label: 'Previous feedback',
      description: 'Classify each previous finding. Call this before structured_output.',
      parameters: {type: 'object', additionalProperties: false, required: ['dispositions'], properties: {
        dispositions: {type: 'array', minItems: prior.length, maxItems: prior.length, items: {
          type: 'object', additionalProperties: false, required: ['id', 'status', 'reason'], properties: {
            id: {enum: prior.map(item => item.id)}, status: {enum: ['resolved', 'accepted', 'still-open']},
            reason: {type: 'string', minLength: 1},
          },
        }},
      }},
      async execute(_id, result) {
        writeFileSync(process.env.REVIEW_FEEDBACK_PATH, JSON.stringify(result.dispositions, null, 2), {mode: 0o600});
        return {content: [{type: 'text', text: 'Feedback saved. Submit the review result now.'}]};
      },
    });
  }
}
