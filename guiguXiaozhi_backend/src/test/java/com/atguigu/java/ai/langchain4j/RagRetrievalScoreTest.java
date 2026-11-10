package com.atguigu.java.ai.langchain4j;

import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.*;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

/**
 * RAG 检索阈值标定用例。
 * <p>
 * 打印各类问题在本地向量库中的相似度得分，用于确定 xiaozhi.rag.min-score：
 * 相关问题应得分 0.80 以上，无关问题（"写诗"/"天气"/"你好"）明显更低，
 * 阈值取在两者之间即可有效避免把无关知识塞进提示词。
 */
@SpringBootTest
public class RagRetrievalScoreTest {

    @Autowired
    private EmbeddingModel embeddingModel;

    @Autowired
    private EmbeddingStore embeddingStore;

    @Test
    public void scoreDistribution() {
        String[] queries = {
                "我最近经常头痛，还有点恶心，应该挂什么科？",
                "孩子反复咳嗽一周，夜间更重，需要做什么检查？",
                "抽血需要空腹吗？",
                "神经内科有哪些专家医生？",
                "我要预约神经内科，2026-11-20上午，姓名陈静",
                "怎么取消挂号",
                "我叫周涛，今年45岁",
                "你好",
                "今天天气怎么样",
                "帮我写一首诗"
        };
        for (String q : queries) {
            Embedding queryEmbedding = embeddingModel.embed(q).content();
            EmbeddingSearchResult<TextSegment> result = embeddingStore.search(
                    EmbeddingSearchRequest.builder().queryEmbedding(queryEmbedding).maxResults(3).build());
            if (result.matches().isEmpty()) {
                System.out.println("### Q=" + q + " -> 无命中");
                continue;
            }
            for (EmbeddingMatch<TextSegment> m : result.matches()) {
                String t = m.embedded().text().replace("\n", " ");
                System.out.println("### Q=" + q + " -> score=" + String.format("%.4f", m.score())
                        + " | " + t.substring(0, Math.min(60, t.length())));
            }
        }
    }
}
