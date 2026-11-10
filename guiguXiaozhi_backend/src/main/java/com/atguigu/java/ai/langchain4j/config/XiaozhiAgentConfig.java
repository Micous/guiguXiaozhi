package com.atguigu.java.ai.langchain4j.config;

import com.atguigu.java.ai.langchain4j.store.MongoChatMemoryStore;
import dev.langchain4j.memory.chat.ChatMemoryProvider;
import dev.langchain4j.memory.chat.MessageWindowChatMemory;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.input.PromptTemplate;
import dev.langchain4j.rag.DefaultRetrievalAugmentor;
import dev.langchain4j.rag.RetrievalAugmentor;
import dev.langchain4j.rag.content.injector.DefaultContentInjector;
import dev.langchain4j.rag.content.retriever.ContentRetriever;
import dev.langchain4j.rag.content.retriever.EmbeddingStoreContentRetriever;
import dev.langchain4j.store.embedding.EmbeddingStore;
import dev.langchain4j.store.embedding.filter.MetadataFilterBuilder;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.List;

@Configuration
public class XiaozhiAgentConfig {

    @Autowired
    private MongoChatMemoryStore mongoChatMemoryStore;

    @Autowired
    private EmbeddingStore embeddingStore;

    @Autowired
    private EmbeddingModel embeddingModel;

    /** 检索返回的最大结果条数 */
    @Value("${xiaozhi.rag.max-results:2}")
    private Integer maxResults;

    /** 检索命中的最低相似度分数 */
    @Value("${xiaozhi.rag.min-score:0.79}")
    private Double minScore;

    /**
     * 专家名单文件不参与聊天 RAG 检索，只通过“查询科室专家”工具按需检索。
     * <p>
     * 否则模型会直接从提示词里的专家段落抄名字（或者抄错名字），
     * 而工具检索出来的名单才是权威来源。
     */
    @Value("${xiaozhi.knowledge.doctor-roster-file:doctors.md}")
    private String doctorRosterFile;

    @Bean
    ChatMemoryProvider chatMemoryProviderXiaozhi() {
        return memoryId -> MessageWindowChatMemory.builder()
                .id(memoryId)
                .maxMessages(20)
                .chatMemoryStore(mongoChatMemoryStore)
                .build();
    }

    /**
     * RAG 内容检索器：从本地内存向量库中检索与用户问题最相关的知识片段。
     */
    @Bean
    ContentRetriever contentRetrieverXiaozhi() {
        return EmbeddingStoreContentRetriever
                .builder()
                // 设置用于生成嵌入向量的嵌入模型（本地 Ollama）
                .embeddingModel(embeddingModel)
                // 指定要使用的嵌入存储（本地内存向量库）
                .embeddingStore(embeddingStore)
                // 设置最大检索结果数量
                .maxResults(maxResults)
                // 设置最小得分阈值，低于该分数的结果不会返回
                .minScore(minScore)
                // 排除专家名单文件：专家信息统一通过“查询科室专家”工具从向量库检索
                .filter(MetadataFilterBuilder.metadataKey("file_name").isNotEqualTo(doctorRosterFile))
                // 构建最终的 EmbeddingStoreContentRetriever 实例
                .build();
    }

    /**
     * RAG 检索增强器。
     * <p>
     * LangChain4j 默认的知识注入模板是英文的
     * “{{userMessage}}\n\nAnswer using the following information:\n{{contents}}”，
     * 由于本地使用的模型参数量远小于云端大模型，英文指令容易诱导模型用英文作答，
     * 因此这里改为中文模板，保证回答语言与系统提示词一致。
     */
    @Bean
    RetrievalAugmentor retrievalAugmentorXiaozhi() {
        DefaultContentInjector contentInjector = DefaultContentInjector.builder()
                .promptTemplate(PromptTemplate.from(
                        "{{userMessage}}\n\n请参考以下医院知识库内容回答用户问题（若知识库内容与你自身的医学知识冲突，请以医学常识为准）：\n\n{{contents}}"))
                .metadataKeysToInclude(List.of())
                .build();

        return DefaultRetrievalAugmentor.builder()
                .contentRetriever(contentRetrieverXiaozhi())
                .contentInjector(contentInjector)
                .build();
    }
}
