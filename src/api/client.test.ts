import { describe, expect, it } from 'vitest';
import { humanizeDetail } from './client';

describe('humanizeDetail', () => {
  it('formats FastAPI validation arrays into field-level Chinese guidance', () => {
    expect(
      humanizeDetail([
        {
          type: 'string_too_short',
          loc: ['body', 'identifier'],
          msg: 'String should have at least 3 characters',
          ctx: { min_length: 3 },
        },
        {
          type: 'string_too_short',
          loc: ['body', 'password'],
          msg: 'String should have at least 8 characters',
          ctx: { min_length: 8 },
        },
      ])
    ).toBe('邮箱或用户名至少需要 3 个字符。 密码至少需要 8 个字符。');
  });
});
