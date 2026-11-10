package com.atguigu.java.ai.langchain4j.config;

import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.model.chat.StreamingChatLanguageModel;
import dev.langchain4j.model.chat.request.ChatRequest;
import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.chat.response.StreamingChatResponseHandler;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;

/**
 * 带重试能力的流式对话模型装饰器。
 * <p>
 * 本地 Ollama 在使用提示词缓存（prompt cache）并让模型直接输出工具调用时，
 * 存在小概率返回“空响应”（HTTP 200 但 content 与 tool_calls 均为空）的现象，
 * 此时前端会收到一个空白的回答。
 * <p>
 * 这里对这种情况做一次自动重试：由于空响应不会产生任何输出片段，
 * 所以重试不会导致内容重复，用户感知不到异常。
 */
public class RetryStreamingChatModel implements StreamingChatLanguageModel {

    private static final Logger log = LoggerFactory.getLogger(RetryStreamingChatModel.class);

    private final StreamingChatLanguageModel delegate;
    private final int maxRetries;

    public RetryStreamingChatModel(StreamingChatLanguageModel delegate, int maxRetries) {
        this.delegate = delegate;
        this.maxRetries = maxRetries;
    }

    @Override
    public void chat(ChatRequest request, StreamingChatResponseHandler handler) {
        attempt(request, handler, 0);
    }

    private void attempt(ChatRequest request, StreamingChatResponseHandler handler, int attempt) {
        delegate.chat(request, new StreamingChatResponseHandler() {

            @Override
            public void onPartialResponse(String token) {
                handler.onPartialResponse(token);
            }

            @Override
            public void onCompleteResponse(ChatResponse response) {
                if (isEmpty(response) && attempt < maxRetries) {
                    log.warn("模型返回空响应（第 {} 次），正在重试", attempt + 1);
                    attempt(request, handler, attempt + 1);
                    return;
                }
                handler.onCompleteResponse(response);
            }

            @Override
            public void onError(Throwable error) {
                handler.onError(error);
            }
        });
    }

    /** 判断这次响应是否为空（既没有文本，也没有工具调用） */
    private boolean isEmpty(ChatResponse response) {
        AiMessage aiMessage = response == null ? null : response.aiMessage();
        if (aiMessage == null) {
            return true;
        }
        String text = aiMessage.text();
        List<?> toolRequests = aiMessage.toolExecutionRequests();
        boolean noText = text == null || text.isBlank();
        boolean noTool = toolRequests == null || toolRequests.isEmpty();
        return noText && noTool;
    }
}
