package com.atguigu.java.ai.langchain4j;

import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.chat.response.StreamingChatResponseHandler;
import dev.langchain4j.model.ollama.OllamaChatModel;
import dev.langchain4j.model.ollama.OllamaStreamingChatModel;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

/**
 * 本地 Ollama 模型测试
 */
@SpringBootTest
public class LLMTest {

    @Autowired
    private OllamaChatModel ollamaChatModel;

    @Test
    public void testOllama() {
        String answer = ollamaChatModel.chat("你是谁？");
        System.out.println(answer);
    }

    @Autowired
    private OllamaStreamingChatModel ollamaStreamingChatModel;

    @Test
    public void testOllamaStreaming() {
        ollamaStreamingChatModel.chat("用一句话介绍北京协和医院", new StreamingChatResponseHandler() {
            @Override
            public void onPartialResponse(String token) {
                System.out.print(token);
            }

            @Override
            public void onCompleteResponse(ChatResponse response) {
                System.out.println("\n[完成]");
            }

            @Override
            public void onError(Throwable error) {
                System.out.println("\n[错误] " + error.getMessage());
            }
        });
    }
}
