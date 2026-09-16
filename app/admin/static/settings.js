// 表单未改动时禁用提交按钮：避免用户以为点了有用、实则提交了原样内容。
// 密码类输入框留空代表"不修改"，因此以"是否非空"而非"是否变化"判断。
(function () {
  function watch(form) {
    var btn = form.querySelector('button[type=submit]');
    if (!btn) return;
    var fields = Array.prototype.slice.call(
      form.querySelectorAll('input, select, textarea'));
    var initial = fields.map(function (f) { return f.value; });

    function changed() {
      return fields.some(function (f, i) {
        // 密码框有任何输入就算改动；其余比对初值
        if (f.type === 'password') return f.value.length > 0;
        return f.value !== initial[i];
      });
    }
    function sync() {
      var dirty = changed();
      btn.disabled = !dirty;
      btn.title = dirty ? '' : '内容未修改';
    }
    fields.forEach(function (f) {
      f.addEventListener('input', sync);
      f.addEventListener('change', sync);
    });
    sync();
  }
  document.querySelectorAll('form[data-dirty-guard]').forEach(watch);
})();
