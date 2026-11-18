# 备课文本风险复核台

备课文本风险复核台用于处理本地合成教研文本，把风险判断落实到**可定位的陈述及其证据状态**：保存来源版本、文本片段、陈述级提醒依据、教师复核结论与教材依据，供校内复核使用。

## 设计原则

- **风险落到句子**：课案按句切分，每条陈述带字符偏移与稳定 ID；风险启发只产生提醒依据（触发理由与置信度），不产生事实结论。
- **置信度不是证据**：新陈述一律为 `unresolved`，模型置信度再高也不会自动解除提醒；证据状态只能由教师显式变更。
- **改写不套用旧证据**：陈述 ID 由句子内容哈希决定。句子被改写、含义变化后 ID 变化，作为全新未解决陈述出现，旧来源不会被套用到新句；仅移动位置、内容不变的句子保留复核结论。
- **来源独立存储、按引用共享**：多个陈述可共用同一条来源；撤销单项确认只解除该陈述的关联，来源在仍被其他陈述引用时保留，最后一个引用解除后才清除。
- **总览与队列同源**：课案总览与待复核队列均从当前持久化状态实时计算，教师确认、补充来源或标记无法核实后两边同步变化。

陈述状态：`unresolved`（待复核）、`confirmed`（已确认，须附来源）、`unverifiable`（无法核实，须写原因）、`not_applicable`（未触发风险的普通陈述）。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：`python -m pip install -r requirements-dev.txt`。

## 运行

导入演练记录并查看处理结果（运行数据保存在 `var/records.json`）：

```bash
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli ingest fixtures/records.json
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli list
```

复核操作：

```bash
# 待复核队列：只列 unresolved 的风险陈述，附触发理由、置信度与定位
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli queue
# 课案总览：各课案与全局的陈述数、待复核/已确认/无法核实计数、风险分
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli overview
# 教师确认（必须附至少一条教材依据，--source 可重复传入共用来源）
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli confirm \
  --record <record_id> --statement <statement_id> \
  --source "七年级上册教学参考|第12页" --note "已核对教参"
# 仅补充来源，不改变状态
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli attach \
  --record <record_id> --statement <statement_id> --source "课程标准2022版|第8页"
# 标记无法核实（必须写原因）
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli unverify \
  --record <record_id> --statement <statement_id> --note "教材无对应数据"
# 撤销教师结论，陈述重新进入待复核队列
PYTHONPATH=src python -m nlp_lesson_risk_triage.cli revoke \
  --record <record_id> --statement <statement_id>
```

`list`、`queue`、`overview` 支持 `--source-filter` 按课案来源过滤。

## 测试

执行 `python -m pytest -q` 检查文本规范化、陈述切分与定位、风险启发、教师复核操作、共享来源引用计数、版本迁移（改写与移动）、总览队列一致性及本地持久化。
