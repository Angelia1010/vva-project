---
name: 办公桌巡查
description: 问到桌面物品、物品摆放是否合规、工位是否整洁、水杯或工牌、人员是否在岗时使用
enabled: true
hotwords: [摆放, 合规, 在岗, 离岗]
rules:
  - condition: 画面中没有人坐在工位上
    cooldown_s: 120
    enabled: true
  - condition: 桌面上出现食物或零食
    cooldown_s: 300
    enabled: false
---

## 检查方法
1. 用 look_now 看清桌面上有哪些物品，列出物品名称。
2. 把物品名称列表交给 desk_compliance，按本技能的规定判断是否合规（不要自己判断）。
3. 问到“我的水杯”或“工牌”时，用 compare_with_reference 对照参考图确认是不是这个物品。
4. 需要说明规定出处时，用 ask_knowledge 查《办公区管理规范》。

## 回答要求
- 合规就说“合规”，不合规就说出具体哪件物品、哪条规定。
- 看不清桌面时，如实说看不清，建议调整摄像头角度。
