package com.atguigu.java.ai.langchain4j.config;

import dev.langchain4j.data.document.Document;
import dev.langchain4j.data.document.Metadata;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.EmbeddingMatch;
import dev.langchain4j.store.embedding.EmbeddingSearchRequest;
import dev.langchain4j.store.embedding.EmbeddingSearchResult;
import dev.langchain4j.store.embedding.EmbeddingStore;
import dev.langchain4j.store.embedding.EmbeddingStoreIngestor;
import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import io.milvus.client.MilvusServiceClient;
import io.milvus.grpc.CollectionSchema;
import io.milvus.grpc.DescribeCollectionResponse;
import io.milvus.grpc.FieldSchema;
import io.milvus.grpc.KeyValuePair;
import io.milvus.param.ConnectParam;
import io.milvus.param.MetricType;
import io.milvus.param.R;
import io.milvus.param.collection.DescribeCollectionParam;
import io.milvus.param.collection.DropCollectionParam;
import io.milvus.param.collection.HasCollectionParam;
import io.milvus.param.collection.FlushParam;
import io.milvus.param.IndexType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 向量存储：使用本地 Docker 部署的 Milvus（standalone 模式）。
 * <p>
 * 相比进程内向量库，Milvus 的好处是索引持久化：知识库只需向量化一次，
 * 后续重启直接复用，不必每次启动都重新embedding。
 * <p>
 * 集合不存在时 LangChain4j 会自动创建；如果更换了向量化模型导致向量维度变化，
 * 这里会先删除旧集合重建，避免维度不匹配导致写入失败。
 */
@Configuration
public class EmbeddingStoreConfig {

    private static final Logger log = LoggerFactory.getLogger(EmbeddingStoreConfig.class);

    @Autowired
    private EmbeddingModel embeddingModel;

    /** Milvus 服务地址 */
    @Value("${xiaozhi.milvus.host:localhost}")
    private String milvusHost;

    @Value("${xiaozhi.milvus.port:19530}")
    private Integer milvusPort;

    /** 集合名 */
    @Value("${xiaozhi.milvus.collection-name:xiaozhi_knowledge}")
    private String collectionName;

    /** 相似度度量方式，COSINE 与之前内存向量库保持一致，阈值可以直接沿用 */
    @Value("${xiaozhi.milvus.metric-type:COSINE}")
    private String metricType;

    @Value("${xiaozhi.milvus.index-type:FLAT}")
    private String indexType;

    /** 集合为空时是否自动灌入知识库 */
    @Value("${xiaozhi.milvus.auto-ingest:true}")
    private Boolean autoIngest;

    /** 知识库文档位置（classpath 通配符） */
    @Value("${xiaozhi.knowledge.location:classpath*:knowledge/*.md}")
    private String knowledgeLocation;

    @Bean
    public EmbeddingStore<TextSegment> embeddingStore() {
        int dimension = embeddingModel.dimension();
        log.info("连接 Milvus: {}:{}，集合={}，向量维度={}", milvusHost, milvusPort, collectionName, dimension);

        alignCollectionDimension(dimension);

        MilvusEmbeddingStore embeddingStore = MilvusEmbeddingStore.builder()
                .host(milvusHost)
                .port(milvusPort)
                .collectionName(collectionName)
                .dimension(dimension)
                .metricType(MetricType.valueOf(metricType.toUpperCase()))
                .indexType(IndexType.valueOf(indexType.toUpperCase()))
                // 关闭逐条 flush：Milvus 对 flush 有 QPS 限制（默认 0.1/s），逐条 flush 会被限流拒绝；
                // 改为灌库完成后统一 flush 一次（见 flushCollection）。
                .autoFlushOnInsert(false)
                .build();

        if (Boolean.TRUE.equals(autoIngest) && isEmpty(embeddingStore)) {
            ingestKnowledgeBase(embeddingStore);
        } else {
            log.info("Milvus 集合中已有数据，跳过知识库灌入（如需重建请先删除集合）");
        }
        return embeddingStore;
    }

    /**
     * 检查已有集合的向量维度是否与当前模型一致，不一致则删除重建。
     */
    private void alignCollectionDimension(int dimension) {
        MilvusServiceClient client = new MilvusServiceClient(ConnectParam.newBuilder()
                .withHost(milvusHost)
                .withPort(milvusPort)
                .build());
        try {
            R<Boolean> has = client.hasCollection(HasCollectionParam.newBuilder()
                    .withCollectionName(collectionName)
                    .build());
            if (has.getData() == null || !has.getData()) {
                log.info("Milvus 集合 {} 不存在，将由 LangChain4j 自动创建", collectionName);
                return;
            }
            R<DescribeCollectionResponse> describe = client.describeCollection(DescribeCollectionParam.newBuilder()
                    .withCollectionName(collectionName)
                    .build());
            CollectionSchema schema = describe.getData().getSchema();
            int existDimension = -1;
            for (FieldSchema field : schema.getFieldsList()) {
                // Milvus 把向量维度放在字段的 type params 里，键名为 "dim"
                for (KeyValuePair kv : field.getTypeParamsList()) {
                    if ("dim".equals(kv.getKey())) {
                        try {
                            existDimension = Integer.parseInt(kv.getValue());
                        } catch (NumberFormatException ignored) {
                            // 非数值型参数，忽略
                        }
                    }
                }
                if (existDimension > 0) {
                    break;
                }
            }
            if (existDimension > 0 && existDimension != dimension) {
                log.warn("集合 {} 的向量维度为 {}，与当前模型的 {} 不一致，将删除并重建集合",
                        collectionName, existDimension, dimension);
                client.dropCollection(DropCollectionParam.newBuilder()
                        .withCollectionName(collectionName)
                        .build());
            }
        } catch (Exception e) {
            log.warn("检查 Milvus 集合维度失败（可能集合尚不存在）：{}", e.getMessage());
        } finally {
            client.close();
        }
    }

    /**
     * 判断集合中是否还没有数据。
     */
    private boolean isEmpty(EmbeddingStore<TextSegment> embeddingStore) {
        try {
            Embedding probe = embeddingModel.embed("__knowledge_probe__").content();
            EmbeddingSearchResult<TextSegment> result = embeddingStore.search(EmbeddingSearchRequest.builder()
                    .queryEmbedding(probe)
                    .maxResults(1)
                    .build());
            return result == null || result.matches().isEmpty();
        } catch (Exception e) {
            log.warn("探测 Milvus 集合是否为空失败：{}", e.getMessage());
            return false;
        }
    }

    /**
     * 把 classpath 下的知识库文档切分、向量化后写入 Milvus。
     */
    private void ingestKnowledgeBase(EmbeddingStore<TextSegment> embeddingStore) {
        List<Document> documents = loadKnowledgeDocuments();
        if (documents.isEmpty()) {
            log.warn("未在 [{}] 下找到知识库文档，RAG 检索将无法命中内容", knowledgeLocation);
            return;
        }
        EmbeddingStoreIngestor.builder()
                .embeddingModel(embeddingModel)
                .embeddingStore(embeddingStore)
                .build()
                .ingest(documents);
        flushCollection();
        log.info("知识库已写入 Milvus 集合 {}，共 {} 个文档", collectionName, documents.size());
    }

    /**
     * 灌库后统一 flush 一次，保证数据立即可被检索（逐条 flush 会被 Milvus 限流）。
     */
    private void flushCollection() {
        MilvusServiceClient client = new MilvusServiceClient(ConnectParam.newBuilder()
                .withHost(milvusHost)
                .withPort(milvusPort)
                .build());
        try {
            client.flush(FlushParam.newBuilder()
                    .addCollectionName(collectionName)
                    .build());
            log.info("Milvus 集合 {} flush 完成", collectionName);
        } catch (Exception e) {
            log.warn("Milvus flush 失败（数据通常仍会由后台自动持久化）：{}", e.getMessage());
        } finally {
            client.close();
        }
    }

    /**
     * 从 classpath 加载知识库文档
     */
    private List<Document> loadKnowledgeDocuments() {
        List<Document> documents = new ArrayList<>();
        try {
            Resource[] resources = new PathMatchingResourcePatternResolver().getResources(knowledgeLocation);
            for (Resource resource : resources) {
                String filename = resource.getFilename();
                if (filename == null) {
                    continue;
                }
                String text;
                try (InputStream in = resource.getInputStream()) {
                    text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
                }
                if (text.isBlank()) {
                    continue;
                }
                Metadata metadata = Metadata.from("file_name", filename);
                documents.add(Document.from(text, metadata));
                log.info("已加载知识库文档：{}", filename);
            }
        } catch (IOException e) {
            log.error("加载知识库文档失败", e);
        }
        return documents;
    }
}
