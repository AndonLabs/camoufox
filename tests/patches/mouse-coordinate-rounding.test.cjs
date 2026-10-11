const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../additions/juggler/input/MouseDispatch.js'), 'utf8');
const context = vm.createContext({ ChromeUtils: { importESModule: () => ({ setTimeout }) } });
vm.runInContext(source.replaceAll('export ', '') + '\nthis.MouseDispatch = MouseDispatch;', context);

for (const scale of [0.75, 1, 1.25, 1.5, 2, 3]) {
  test(`native input stays inside the renderer at scale ${scale}`, () => {
    const box = { left: 0.2, top: 51.4, width: 960, height: 640 };
    const widget = {
      left: box.left * scale, top: box.top * scale,
      right: (box.left + box.width) * scale, bottom: (box.top + box.height) * scale,
      width: box.width * scale, height: box.height * scale,
    };
    const win = { devicePixelRatio: 7, windowUtils: { toTopLevelWidgetRect: () => widget } };
    const dispatch = new context.MouseDispatch(win, box);
    for (const [x, y] of [[0, 0], [959.99, 639.99], [448, 300]]) {
      const actual = dispatch.toAbsolute(x, y);
      const deviceX = Math.round(actual.x * scale);
      const deviceY = Math.round(actual.y * scale);
      assert.ok(deviceX >= widget.left && deviceX < widget.right, `x=${deviceX}`);
      assert.ok(deviceY >= widget.top && deviceY < widget.bottom, `y=${deviceY}`);
      assert.ok(Math.abs(actual.x - box.left - x) * scale < 1);
      assert.ok(Math.abs(actual.y - box.top - y) * scale < 1);
      if (x === 448) assert.deepEqual({ ...actual }, { x: x + box.left, y: y + box.top });
    }
  });
}
