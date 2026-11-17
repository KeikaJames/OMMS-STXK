// 用 Node 自带的 acorn 按 ES5 语法解析前端脚本：node --expose-internals tools/check_es5.js 文件...
'use strict';

var fs = require('fs');
var acorn;
try {
  acorn = require('internal/deps/acorn/acorn/dist/acorn');
} catch (error) {
  console.log('SKIP acorn');
  process.exit(0);
}

var failed = 0;
process.argv.slice(2).forEach(function (path) {
  try {
    acorn.parse(fs.readFileSync(path, 'utf8'), { ecmaVersion: 5, sourceType: 'script' });
  } catch (error) {
    failed += 1;
    console.log(path + ': ' + error.message);
  }
});
process.exit(failed ? 1 : 0);
