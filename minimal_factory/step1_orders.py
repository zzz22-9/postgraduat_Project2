# 第一步：用一个字典表示一笔订单。
# 字典里的每一项都是“字段名: 值”。
order = {
    "order_id": "ORD-001",       # 订单编号
    "product_id": "P001",        # 产品编号
    "product_name": "简易装配件",  # 产品名称
    "quantity": 1,               # 要生产的数量
    "status": "待生产",           # 订单当前的状态
}

# print 把信息显示在终端里；order["字段名"] 取出对应的值。
print("这是我的第一笔订单：")
print("订单编号：", order["order_id"])
print("产品编号：", order["product_id"])
print("产品名称：", order["product_name"])
print("生产数量：", order["quantity"])
print("订单状态：", order["status"])

# 练习：把上面的 quantity 从 1 改成 3，保存后重新运行。
