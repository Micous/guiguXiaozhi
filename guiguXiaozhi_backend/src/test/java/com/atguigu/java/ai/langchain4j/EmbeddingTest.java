package com.atguigu.java.ai.langchain4j;

import dev.langchain4j.data.document.Document;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.output.Response;
import dev.langchain4j.store.embedding.*;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 向量化与本地向量库测试
 */
@SpringBootTest
public class EmbeddingTest {

    @Autowired
    private EmbeddingModel embeddingModel;

    @Test
    public void testEmbeddingModel() {
        Response<Embedding> embed = embeddingModel.embed("你好");
        System.out.println("向量维度：" + embed.content().vector().length);
    }

    @Autowired
    private EmbeddingStore embeddingStore;

    /**
     * 将文本转换成向量，然后存储到本地内存向量库中
     */
    @Test
    public void testEmbeded() {
        TextSegment segment1 = TextSegment.from("我喜欢羽毛球");
        Embedding embedding1 = embeddingModel.embed(segment1).content();
        embeddingStore.add(embedding1, segment1);
        TextSegment segment2 = TextSegment.from("今天天气很好");
        Embedding embedding2 = embeddingModel.embed(segment2).content();
        embeddingStore.add(embedding2, segment2);
    }

    /**
     * 相似度匹配
     */
    @Test
    public void embeddingSearch() {
        Embedding queryEmbedding = embeddingModel.embed("你最喜欢的运动是什么？").content();
        EmbeddingSearchRequest searchRequest = EmbeddingSearchRequest.builder()
                .queryEmbedding(queryEmbedding)
                .maxResults(1)
                .build();
        EmbeddingSearchResult<TextSegment> searchResult = embeddingStore.search(searchRequest);
        if (searchResult.matches().isEmpty()) {
            System.out.println("没有匹配到结果");
            return;
        }
        EmbeddingMatch<TextSegment> embeddingMatch = searchResult.matches().get(0);
        System.out.println("相似度得分：" + embeddingMatch.score());
        System.out.println("匹配内容：" + embeddingMatch.embedded().text());
    }

    /**
     * 将 classpath 下的知识库文档写入本地向量库
     */
    @Test
    public void testUploadKnowledgeLibrary() throws Exception {
        List<Document> documents = new ArrayList<>();
        Resource[] resources = new PathMatchingResourcePatternResolver().getResources("classpath*:knowledge/*.md");
        for (Resource resource : resources) {
            try (InputStream in = resource.getInputStream()) {
                String text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
                documents.add(Document.from(text, dev.langchain4j.data.document.Metadata.from("file_name", resource.getFilename())));
            }
        }
        System.out.println("共加载文档数：" + documents.size());
        EmbeddingStoreIngestor
                .builder()
                .embeddingStore(embeddingStore)
                .embeddingModel(embeddingModel)
                .build()
                .ingest(documents);
    }
}
