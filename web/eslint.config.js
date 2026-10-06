import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['src/generated/**'] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // 不可信内容一律当文本渲染：这个属性是 XSS 的入口。
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: '消息、工具输出与产物是不可信内容，按文本渲染，不要注入 HTML。',
        },
        {
          selector: "MemberExpression[property.name='innerHTML']",
          message: '不要写 innerHTML：不可信内容只能按文本进页面。',
        },
      ],
    },
  },
);
