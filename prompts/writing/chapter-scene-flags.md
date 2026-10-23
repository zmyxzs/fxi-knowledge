这是一份已经完成的章纲，仅少数场景缺少 dialogue_expected。依据完整提案，判断这些场景是否安排了需要展开的对白。true 表示预期有对白，false 表示未安排对白；这个标记不替正文决定具体台词。

只为 missing_scene_indices 中每个一基场次返回一个布尔值。既有对白标记和所有剧情、人物、次序、措辞均保留，不补写新事件、对白或解释。返回符合 output_schema 的 JSON 对象，仅含 scene_flags 数组，每项仅含 scene_index 与 dialogue_expected。
