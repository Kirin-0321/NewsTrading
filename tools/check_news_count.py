import json

# 读取文件
with open('data/cleaned/12-25_merged_clear.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# 统计
actual_count = len(data['news'])
metadata_merged = data['metadata']['merged_count']
metadata_removed = data['metadata']['duplicates_removed']
metadata_final = data['metadata']['final_count']

print(f"元数据统计：")
print(f"  合并前总数: {metadata_merged}")
print(f"  去重数量: {metadata_removed}")
print(f"  最终数量: {metadata_final}")
print(f"  计算验证: {metadata_merged} - {metadata_removed} = {metadata_merged - metadata_removed}")
print()
print(f"实际统计：")
print(f"  文件中实际新闻数量: {actual_count}")
print()
print(f"结论：")
if actual_count == metadata_final:
    print(f"  ✅ 元数据记录正确（{metadata_final} == {actual_count}）")
else:
    print(f"  ❌ 元数据记录错误（记录{metadata_final}，实际{actual_count}）")

